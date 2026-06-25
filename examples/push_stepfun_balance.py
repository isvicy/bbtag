#!/usr/bin/env python3
"""Render StepFun account balance for 2.13-inch tags.

默认行为:
1. 从 STEPFUN_API_KEY 或 --api-key 读取令牌
2. 请求 GET {base_url}/accounts
3. 生成 250x122 的余额面板
4. 保存预览图
5. 推送到 2.13 寸设备

示例:
    uv run examples/push_stepfun_balance.py --preview-only
    uv run examples/push_stepfun_balance.py --device EDP-F3F4F5F6
    uv run examples/push_stepfun_balance.py --input-json examples/fixtures/stepfun_account.sample.json --preview-only
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

from bluetag.ble import BleDependencyError
from bluetag.image import layer_to_bytes, process_bicolor_image
from bluetag.screens import get_screen_profile
from bluetag.transfer import send_bicolor_image
from stepfun_account_common import (
    AccountView,
    StepFunAccountError,
    compute_cash_ratio,
    default_base_url,
    fetch_account_json,
    format_money,
    format_money_console,
    format_type_label,
    format_ratio_console,
    format_updated_text,
    parse_account_payload,
    resolve_api_key,
)

DEFAULT_OUTPUT = "StepFun-balance-2.13inch.png"
DEFAULT_SCREEN = "2.13inch"
DEFAULT_SCAN_TIMEOUT = 12.0
DEFAULT_SCAN_RETRIES = 3
DEFAULT_CONNECT_RETRIES = 3

MONO_FONT_SEARCH = [
    "/System/Library/Fonts/Supplemental/Menlo.ttc",
    "/System/Library/Fonts/Monaco.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    "/usr/share/fonts/TTF/DejaVuSansMono.ttf",
    "C:\\Windows\\Fonts\\consola.ttf",
]


def load_font(size: int, *, font_path: str | None = None) -> ImageFont.FreeTypeFont:
    if font_path:
        return ImageFont.truetype(font_path, size)
    for path in MONO_FONT_SEARCH:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _new_crisp_canvas(width: int, height: int) -> tuple[Image.Image, ImageDraw.ImageDraw]:
    img = Image.new("1", (width, height), 1)
    draw = ImageDraw.Draw(img)
    draw.fontmode = "1"
    return img, draw


def _text_size(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont) -> tuple[int, int]:
    bbox = draw.textbbox((0, 0), text, font=font)
    return bbox[2] - bbox[0], bbox[3] - bbox[1]


def draw_progress_bar(
    draw: ImageDraw.ImageDraw,
    *,
    x: int,
    y: int,
    width: int,
    height: int,
    percent: float,
):
    draw.rectangle((x, y, x + width, y + height), outline="black", width=1)
    inner_x0 = x + 2
    inner_y0 = y + 2
    inner_x1 = x + width - 1
    inner_y1 = y + height - 1
    inner_width = max(0, inner_x1 - inner_x0)
    fill_width = round(inner_width * max(0.0, min(100.0, percent)) / 100.0)

    if fill_width > 0:
        draw.rectangle(
            (inner_x0, inner_y0, inner_x0 + fill_width - 1, inner_y1),
            fill="black",
        )


def render_account_image(
    account: AccountView,
    *,
    width: int = 250,
    height: int = 122,
    font_path: str | None = None,
) -> Image.Image:
    img, draw = _new_crisp_canvas(width, height)
    ratio = compute_cash_ratio(account)

    title_font = load_font(12, font_path=font_path)
    ratio_font = load_font(10, font_path=font_path)
    footer_font = load_font(11, font_path=font_path)
    detail_font = load_font(10, font_path=font_path)

    left_pad = 8
    right_pad = 8
    top_pad = 3
    bottom_pad = 4
    bar_h = 14

    title_text = "StepFun"
    title_w, title_h = _text_size(draw, title_text, title_font)
    draw.text(((width - title_w) // 2, top_pad), title_text, fill=0, font=title_font)

    bar_x = left_pad
    bar_width = width - left_pad - right_pad
    ratio_y = top_pad + title_h + 6
    ratio_w, ratio_line_h = _text_size(draw, ratio.summary_text, ratio_font)
    draw.text(
        (bar_x + bar_width - ratio_w, ratio_y),
        ratio.summary_text,
        fill=0,
        font=ratio_font,
    )

    bar_y = ratio_y + ratio_line_h + 3
    draw_progress_bar(
        draw,
        x=bar_x,
        y=bar_y,
        width=bar_width,
        height=bar_h,
        percent=ratio.used_percent,
    )

    updated_text = format_updated_text(account.fetched_at)
    _, updated_h = _text_size(draw, updated_text, detail_font)
    footer_top = height - bottom_pad - updated_h

    divider_y = bar_y + bar_h + 8
    draw.line((left_pad, divider_y, width - right_pad, divider_y), fill=0, width=1)

    meta_y = divider_y + 5
    type_text = f"TYPE {format_type_label(account.account_type)}"
    cash_text = f"CASH {format_money(account.cash_total)}"
    draw.text((left_pad, meta_y), type_text, fill=0, font=footer_font)
    cash_w, _ = _text_size(draw, cash_text, footer_font)
    draw.text((width - right_pad - cash_w, meta_y), cash_text, fill=0, font=footer_font)

    updated_w, _ = _text_size(draw, updated_text, detail_font)
    draw.text(
        (width - right_pad - updated_w, footer_top),
        updated_text,
        fill=0,
        font=detail_font,
    )

    return img.convert("RGB")


def save_preview(image: Image.Image, output_path: Path) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    image.save(output_path)
    return output_path


def _save_device(device: dict, profile):
    profile.cache_path.write_text(f"{device['name']}\n{device['address']}\n")


def _load_device(profile) -> dict | None:
    if not profile.cache_path.exists():
        return None
    lines = profile.cache_path.read_text().strip().splitlines()
    if len(lines) >= 2:
        return {"name": lines[0], "address": lines[1]}
    return None


async def _find_target(args, profile) -> dict | None:
    from bluetag.ble import find_device

    cached = None
    search_name = args.device
    search_address = args.address
    if not search_name and not search_address:
        cached = _load_device(profile)
        if cached:
            print(
                f"使用 {profile.name} 缓存设备作为扫描目标: "
                f"{cached['name']} ({cached['address']})"
            )
            search_name = cached["name"]
            search_address = cached["address"]

    print(
        f"扫描 {profile.name} 设备 "
        f"({profile.device_prefix}*, {args.scan_timeout:.1f}s/次)..."
    )
    target = await find_device(
        device_name=search_name,
        device_address=search_address,
        timeout=args.scan_timeout,
        scan_retries=DEFAULT_SCAN_RETRIES,
        prefixes=(profile.device_prefix,),
    )
    if target:
        _save_device(target, profile)
        return target

    if cached:
        print("未扫描到缓存设备，改为搜索任意同型号设备...")
        target = await find_device(
            timeout=args.scan_timeout,
            scan_retries=DEFAULT_SCAN_RETRIES,
            prefixes=(profile.device_prefix,),
        )
        if target:
            _save_device(target, profile)
            return target

    return None


def _layer_progress(layer_name: str, sent: int, total: int):
    if sent == total:
        print(f"\r✅ {layer_name}发送完成! ({total} 包)")
    elif sent == 1 or sent % 10 == 0:
        print(f"\r  {layer_name}发送中 {sent}/{total}...", end="", flush=True)


async def push_image_to_small_screen(image: Image.Image, args) -> bool:
    from bluetag.ble import connect_session

    profile = get_screen_profile(args.screen)
    interval_ms = args.interval or profile.default_interval_ms

    black_layer, red_layer, _preview = process_bicolor_image(
        image,
        profile.name,
        threshold=128,
        dither=False,
        rotate=profile.rotate,
        mirror=profile.mirror,
        swap_wh=profile.swap_wh,
        detect_red=profile.detect_red,
    )
    black_data = layer_to_bytes(black_layer, profile.encoding)
    red_data = layer_to_bytes(red_layer, profile.encoding)

    target = await _find_target(args, profile)
    if not target:
        print("❌ 未找到设备")
        return False

    session = await connect_session(
        target.get("_ble_device") or target["address"],
        timeout=20.0,
        connect_retries=DEFAULT_CONNECT_RETRIES,
    )
    if not session:
        print("❌ 连接设备失败")
        return False

    try:
        print(
            f"连接 {target['name']} [{profile.name}], "
            f"黑层 {len(black_data)} bytes, 红层 {len(red_data)} bytes"
        )
        ok = await send_bicolor_image(
            session,
            black_data,
            red_data,
            delay_ms=interval_ms,
            settle_ms=profile.settle_ms,
            flush_every=profile.flush_every,
            on_progress=_layer_progress,
        )
        if not ok:
            print("❌ 发送失败")
        return ok
    finally:
        await session.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="把 StepFun 账户余额画成 2.13 寸电子价签样式并推送。",
    )
    parser.add_argument("--screen", default=DEFAULT_SCREEN, help="屏幕尺寸，默认 2.13inch")
    parser.add_argument("--device", "-d", help="设备名，例如 EDP-F3F4F5F6")
    parser.add_argument("--address", "-a", help="设备 BLE 地址，优先于 --device")
    parser.add_argument("--interval", "-i", type=int, help="包间隔 (ms)")
    parser.add_argument("--preview-only", action="store_true", help="只生成图片，不推送")
    parser.add_argument("--output", default=DEFAULT_OUTPUT, help="预览图输出路径")
    parser.add_argument("--input-json", type=Path, help="本地 account JSON，跳过网络")
    parser.add_argument("--api-key", help="覆盖 STEPFUN_API_KEY")
    parser.add_argument("--base-url", help=f"API base URL，默认 {default_base_url()}")
    parser.add_argument("--timeout", type=float, default=30.0, help="HTTP 超时秒数")
    parser.add_argument(
        "--scan-timeout",
        type=float,
        default=DEFAULT_SCAN_TIMEOUT,
        help="BLE 扫描超时秒数",
    )
    parser.add_argument("--font", help="自定义等宽字体路径")
    return parser.parse_args()


def load_account_view(args: argparse.Namespace) -> tuple[AccountView, str]:
    if args.input_json:
        try:
            payload = json.loads(args.input_json.read_text(encoding="utf-8"))
        except OSError as exc:
            raise StepFunAccountError(f"Failed to read input JSON: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise StepFunAccountError(f"Invalid input JSON: {exc}") from exc
        account = parse_account_payload(payload)
        return account, f"file:{args.input_json}"

    api_key = resolve_api_key(args.api_key)
    base_url = args.base_url or default_base_url()
    payload = fetch_account_json(base_url, api_key, args.timeout)
    account = parse_account_payload(payload)
    return account, f"{base_url.rstrip('/')}/accounts"


def print_account_summary(account: AccountView):
    fmt = format_money_console
    ratio = compute_cash_ratio(account)
    print(f"  ratio: {format_ratio_console(ratio)}")
    print(f"  type: {format_type_label(account.account_type)}")
    print(f"  balance: {fmt(account.balance)}")
    print(f"  cash: {fmt(account.cash_total)}")
    print(f"  voucher: {fmt(account.voucher_total)}")


def main() -> int:
    args = parse_args()
    profile = get_screen_profile(args.screen)
    if profile.name != "2.13inch":
        print("❌ 当前脚本只为 2.13 寸布局设计，请使用 --screen 2.13inch", file=sys.stderr)
        return 2

    try:
        account, source = load_account_view(args)
        image = render_account_image(
            account,
            width=profile.width,
            height=profile.height,
            font_path=args.font,
        )
        output_path = save_preview(image, Path(args.output))
        print(f"预览已保存: {output_path}")
        print(f"Account 来源: {source}")
        print_account_summary(account)

        if args.preview_only:
            return 0

        try:
            ok = asyncio.run(push_image_to_small_screen(image, args))
        except BleDependencyError as exc:
            print(f"❌ {exc}", file=sys.stderr)
            return 2
        return 0 if ok else 1
    except StepFunAccountError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
