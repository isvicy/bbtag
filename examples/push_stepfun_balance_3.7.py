#!/usr/bin/env python3
"""Render StepFun account balance for 3.7-inch tags (landscape).

默认行为:
1. 从 STEPFUN_API_KEY 或 --api-key 读取令牌
2. 请求 GET {base_url}/accounts
3. 生成 416x240 的余额面板 (横屏布局)
4. 保存预览图
5. 推送到 3.7 寸设备

示例:
    uv run examples/push_stepfun_balance_3.7.py --preview-only
    uv run examples/push_stepfun_balance_3.7.py --device EPD-D984FADA
    uv run examples/push_stepfun_balance_3.7.py --input-json examples/fixtures/stepfun_account.sample.json --preview-only
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

from bluetag import build_frame, pack_2bpp, packetize, quantize
from bluetag.ble import BleDependencyError
from bluetag.protocol import parse_mac_suffix
from bluetag.screens import get_screen_profile
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
    format_updated_text_long,
    parse_account_payload,
    resolve_api_key,
)

DEFAULT_OUTPUT = "StepFun-balance-3.7inch.png"
DEFAULT_SCREEN = "3.7inch"
DEFAULT_SCAN_TIMEOUT = 12.0
DEFAULT_SCAN_RETRIES = 3
DEFAULT_CONNECT_RETRIES = 3

WIDTH = 416
HEIGHT = 240

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


def _text_size(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont) -> tuple[int, int]:
    bbox = draw.textbbox((0, 0), text, font=font)
    return bbox[2] - bbox[0], bbox[3] - bbox[1]


def _draw_centered(
    draw: ImageDraw.ImageDraw,
    text: str,
    *,
    y: int,
    width: int,
    font: ImageFont.ImageFont,
    fill: str = "black",
):
    text_w, _ = _text_size(draw, text, font)
    draw.text(((width - text_w) // 2, y), text, fill=fill, font=font)


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


def _draw_footer_column(
    draw: ImageDraw.ImageDraw,
    *,
    x: int,
    y: int,
    width: int,
    label: str,
    value: str,
    label_font: ImageFont.ImageFont,
    value_font: ImageFont.ImageFont,
):
    label_w, label_h = _text_size(draw, label, label_font)
    value_w, value_h = _text_size(draw, value, value_font)
    draw.text((x + (width - label_w) // 2, y), label, fill="black", font=label_font)
    draw.text(
        (x + (width - value_w) // 2, y + label_h + 6),
        value,
        fill="black",
        font=value_font,
    )


def render_account_image(
    account: AccountView,
    *,
    width: int = WIDTH,
    height: int = HEIGHT,
    font_path: str | None = None,
) -> Image.Image:
    img = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(img)
    ratio = compute_cash_ratio(account)

    title_font = load_font(22, font_path=font_path)
    ratio_font = load_font(18, font_path=font_path)
    col_label_font = load_font(14, font_path=font_path)
    col_value_font = load_font(18, font_path=font_path)
    detail_font = load_font(13, font_path=font_path)

    left_pad = 24
    right_pad = 24
    top_pad = 12
    bottom_pad = 12
    bar_h = 22

    title_text = "StepFun"
    _, title_h = _text_size(draw, title_text, title_font)
    _draw_centered(draw, title_text, y=top_pad, width=width, font=title_font)

    bar_x = left_pad
    bar_width = width - left_pad - right_pad
    ratio_y = top_pad + title_h + 14
    ratio_w, ratio_line_h = _text_size(draw, ratio.summary_text, ratio_font)
    draw.text(
        (bar_x + bar_width - ratio_w, ratio_y),
        ratio.summary_text,
        fill="black",
        font=ratio_font,
    )

    bar_y = ratio_y + ratio_line_h + 6
    draw_progress_bar(
        draw,
        x=bar_x,
        y=bar_y,
        width=bar_width,
        height=bar_h,
        percent=ratio.used_percent,
    )

    divider_y = bar_y + bar_h + 16
    draw.line((left_pad, divider_y, width - right_pad, divider_y), fill="black", width=1)

    footer_top = divider_y + 12
    col_gap = 24
    col_w = (bar_width - col_gap) // 2
    _draw_footer_column(
        draw,
        x=left_pad,
        y=footer_top,
        width=col_w,
        label="TYPE",
        value=format_type_label(account.account_type),
        label_font=col_label_font,
        value_font=col_value_font,
    )
    _draw_footer_column(
        draw,
        x=left_pad + col_w + col_gap,
        y=footer_top,
        width=col_w,
        label="CASH",
        value=format_money(account.cash_total),
        label_font=col_label_font,
        value_font=col_value_font,
    )

    updated_text = format_updated_text_long(account.fetched_at)
    updated_w, updated_h = _text_size(draw, updated_text, detail_font)
    draw.text(
        (width - right_pad - updated_w, height - bottom_pad - updated_h),
        updated_text,
        fill="black",
        font=detail_font,
    )

    return img


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


def _on_progress(sent: int, total: int):
    if sent == total:
        print(f"\r✅ 发送完成! ({total} 包)")
    elif sent == 1 or sent % 10 == 0:
        print(f"\r  发送中 {sent}/{total}...", end="", flush=True)


def prepare_landscape_image_for_37_screen(
    image: Image.Image,
    profile,
) -> Image.Image:
    if image.size != (WIDTH, HEIGHT):
        image = image.convert("RGB").resize((WIDTH, HEIGHT), Image.LANCZOS)
    else:
        image = image.convert("RGB")

    native = image.transpose(Image.Transpose.ROTATE_90)
    if native.size != profile.size:
        native = native.resize(profile.size, Image.LANCZOS)
    return native


async def push_image_to_37_screen(image: Image.Image, args) -> bool:
    from bluetag.ble import connect_session

    profile = get_screen_profile(args.screen)
    interval_ms = args.interval or profile.default_interval_ms

    native_img = prepare_landscape_image_for_37_screen(image, profile)
    indices = quantize(native_img, flip=profile.mirror, size=profile.size)
    data_2bpp = pack_2bpp(indices)

    target = await _find_target(args, profile)
    if not target:
        print("❌ 未找到设备")
        return False

    mac_suffix = parse_mac_suffix(target["name"])
    frame = build_frame(mac_suffix, data_2bpp)
    packets = packetize(frame)

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
            f"帧数据 {len(frame)} bytes, {len(packets)} 包"
        )
        total = len(packets)
        for index, packet in enumerate(packets, start=1):
            await session.write(packet, response=False)
            await asyncio.sleep(interval_ms / 1000.0)
            _on_progress(index, total)
        return True
    except Exception as exc:
        print(f"\n❌ 发送失败: {exc}")
        return False
    finally:
        await session.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="把 StepFun 账户余额画成 3.7 寸电子价签样式并推送 (横屏布局)。",
    )
    parser.add_argument("--screen", default=DEFAULT_SCREEN, help="屏幕尺寸，默认 3.7inch")
    parser.add_argument("--device", "-d", help="设备名，例如 EPD-D984FADA")
    parser.add_argument("--address", "-a", help="设备 BLE 地址")
    parser.add_argument("--interval", "-i", type=int, help="包间隔 (ms)")
    parser.add_argument("--preview-only", action="store_true", help="只生成图片")
    parser.add_argument("--output", default=DEFAULT_OUTPUT, help="预览图输出路径")
    parser.add_argument("--input-json", type=Path, help="本地 account JSON")
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
    if profile.name != "3.7inch":
        print("❌ 当前脚本只为 3.7 寸布局设计，请使用 --screen 3.7inch", file=sys.stderr)
        return 2

    try:
        account, source = load_account_view(args)
        image = render_account_image(account, font_path=args.font)
        output_path = save_preview(image, Path(args.output))
        print(f"预览已保存: {output_path}")
        print(f"Account 来源: {source}")
        print_account_summary(account)

        if args.preview_only:
            return 0

        try:
            ok = asyncio.run(push_image_to_37_screen(image, args))
        except BleDependencyError as exc:
            print(f"❌ {exc}", file=sys.stderr)
            return 2
        return 0 if ok else 1
    except StepFunAccountError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
