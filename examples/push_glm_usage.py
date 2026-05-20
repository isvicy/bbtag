#!/usr/bin/env python3
"""Render GLM Coding Plan usage in a compact /stats-like layout for 2.13-inch tags.

默认行为:
1. 从 Claude Code settings / 环境变量读取 ANTHROPIC_* 凭证
2. 请求 GET {root}/api/monitor/usage/quota/limit
3. 生成 250x122 的 usage 面板
4. 保存预览图
5. 推送到 2.13 寸设备

示例:
    uv run examples/push_glm_usage.py --preview-only
    uv run examples/push_glm_usage.py --device EDP-F3F4F5F6
    uv run examples/push_glm_usage.py --input-json quota.json --preview-only
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import urllib.error
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

from bluetag.ble import BleDependencyError
from glm_quota_common import GlmQuotaParseError, parse_display_rows
from bluetag.image import layer_to_bytes, process_bicolor_image
from bluetag.screens import get_screen_profile
from bluetag.transfer import send_bicolor_image

QUOTA_PATH = "/api/monitor/usage/quota/limit"
DEFAULT_OUTPUT = "glm-usage-2.13inch.png"
DEFAULT_SCREEN = "2.13inch"
DEFAULT_SCAN_TIMEOUT = 12.0
DEFAULT_SCAN_RETRIES = 3
DEFAULT_CONNECT_RETRIES = 3
DEFAULT_ZHIPU_ROOT = "https://open.bigmodel.cn"

TOKEN_LABELS = ("5h limit", "weekly limit")

MONO_FONT_SEARCH = [
    "/System/Library/Fonts/Supplemental/Menlo.ttc",
    "/System/Library/Fonts/Monaco.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    "/usr/share/fonts/TTF/DejaVuSansMono.ttf",
    "C:\\Windows\\Fonts\\consola.ttf",
]

SETTINGS_PATHS = (
    Path.home() / ".claude" / "settings.json",
    Path.home() / ".claude" / "settings.local.json",
)


class GlmUsageError(RuntimeError):
    """Raised when the script cannot load credentials or fetch usage."""


@dataclass
class UsageRow:
    label: str
    left_percent: float
    resets_text: str
    stat_text: str | None = None


# =============================================================================
# 凭证 / 配置
# =============================================================================

def _read_settings_env(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    env = data.get("env")
    if not isinstance(env, dict):
        return {}
    return {k: str(v) for k, v in env.items() if isinstance(v, str) and v}


def load_merged_claude_env(
    cwd: Path | None = None,
    extra_settings: Path | None = None,
) -> dict[str, str]:
    merged: dict[str, str] = {}
    root = cwd or Path.cwd()
    paths = [
        *SETTINGS_PATHS,
        root / ".claude" / "settings.json",
        root / ".claude" / "settings.local.json",
    ]
    if extra_settings:
        paths.append(extra_settings)

    for path in paths:
        merged = {**merged, **_read_settings_env(path)}

    for key, value in os.environ.items():
        if value:
            merged[key] = value
    return merged


def _first_env(keys: Sequence[str], merged: dict[str, str]) -> str | None:
    for key in keys:
        val = merged.get(key, "").strip()
        if val:
            return val
    return None


def normalize_auth_header(token: str) -> str:
    trimmed = token.strip()
    if trimmed.lower().startswith("bearer "):
        return trimmed
    return f"Bearer {trimmed}"


def monitor_root_from_anthropic_url(base_url: str) -> str:
    try:
        parsed = urlparse(base_url.strip())
    except ValueError as exc:
        raise GlmUsageError(f"Invalid ANTHROPIC_BASE_URL: {base_url}") from exc

    host = (parsed.hostname or "").lower()
    if not host:
        raise GlmUsageError(f"Invalid ANTHROPIC_BASE_URL: {base_url}")

    if "api.z.ai" in host or host == "z.ai":
        return f"{parsed.scheme}://{parsed.hostname}"
    if "bigmodel.cn" in host:
        return f"{parsed.scheme}://{parsed.hostname}"

    raise GlmUsageError(
        "Unrecognized ANTHROPIC_BASE_URL host. Supported examples:\n"
        "  https://open.bigmodel.cn/api/anthropic\n"
        "  https://api.z.ai/api/anthropic"
    )


def resolve_credentials(
    merged: dict[str, str],
    *,
    auth_token: str | None = None,
    base_url: str | None = None,
) -> tuple[str, str]:
    token = (auth_token or "").strip() or _first_env(
        ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY"),
        merged,
    )
    if not token:
        token = _first_env(
            ("ZHIPU_API_KEY", "ZHIPUAI_API_KEY", "BIGMODEL_API_KEY"),
            merged,
        )
    if not token:
        raise GlmUsageError(
            "ANTHROPIC_AUTH_TOKEN is not set. Configure GLM Coding Plan in "
            "~/.claude/settings.json under env.ANTHROPIC_AUTH_TOKEN, or pass --auth-token."
        )

    if base_url:
        root = base_url.strip().rstrip("/")
    else:
        explicit_root = _first_env(
            ("ZHIPU_BASE_URL", "BIGMODEL_BASE_URL", "PULSE_ZHIPU_BASE_URL"),
            merged,
        )
        if explicit_root:
            root = explicit_root.rstrip("/")
        else:
            anthropic_base = _first_env(("ANTHROPIC_BASE_URL",), merged)
            if not anthropic_base:
                root = DEFAULT_ZHIPU_ROOT
            else:
                root = monitor_root_from_anthropic_url(anthropic_base)

    return token, root


# =============================================================================
# API
# =============================================================================

def fetch_quota_json(root_url: str, api_key: str, timeout: float) -> dict[str, Any]:
    url = f"{root_url.rstrip('/')}{QUOTA_PATH}"
    headers = {
        "Authorization": normalize_auth_header(api_key),
        "User-Agent": "push_glm_usage.py",
        "Accept": "application/json",
        "Accept-Language": "en-US,en",
    }

    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise GlmUsageError(
                "Authentication failed with 401/403. Check ANTHROPIC_AUTH_TOKEN in "
                "~/.claude/settings.json."
            ) from exc
        details = exc.read().decode("utf-8", errors="replace").strip()
        suffix = f": {details}" if details else ""
        raise GlmUsageError(f"GLM API returned HTTP {exc.code}{suffix}") from exc
    except urllib.error.URLError as exc:
        raise GlmUsageError(f"Request failed: {exc.reason}") from exc

    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise GlmUsageError(f"Failed to parse API response as JSON: {exc}") from exc

    if not isinstance(payload, dict):
        raise GlmUsageError("Expected a JSON object from quota/limit.")
    return payload


# =============================================================================
# 数据转换
# =============================================================================

def resolve_timezone(name: str | None):
    if not name:
        return datetime.now().astimezone().tzinfo or timezone.utc
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise GlmUsageError(f"Unknown timezone: {name}") from exc


def format_reset_text(resets_at: str | None, tzinfo) -> str:
    if not resets_at:
        return "resets unknown"

    iso_value = resets_at.replace("Z", "+00:00")
    try:
        reset_dt = datetime.fromisoformat(iso_value).astimezone(tzinfo)
    except ValueError:
        return "resets unknown"

    now_dt = datetime.now(tzinfo)
    time_text = reset_dt.strftime("%H:%M")
    if reset_dt.date() == now_dt.date():
        return f"resets {time_text}"
    if reset_dt.year == now_dt.year:
        return f"resets {time_text} on {reset_dt.day} {reset_dt.strftime('%b')}"
    return f"resets {time_text} on {reset_dt:%Y-%m-%d}"


def build_rows(payload: dict[str, Any], tzinfo) -> list[UsageRow]:
    try:
        parsed = parse_display_rows(
            payload,
            labels=TOKEN_LABELS,
            mcp_stat=lambda remaining, total: f"{remaining} left",
        )
    except GlmQuotaParseError as exc:
        raise GlmUsageError(str(exc)) from exc
    return [
        UsageRow(
            label=row.label,
            left_percent=row.left_percent,
            resets_text=format_reset_text(row.reset_at, tzinfo),
            stat_text=row.stat_text,
        )
        for row in parsed
    ]


# =============================================================================
# 渲染
# =============================================================================

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


def render_usage_image(
    rows: list[UsageRow],
    *,
    width: int = 250,
    height: int = 122,
    font_path: str | None = None,
) -> Image.Image:
    img, draw = _new_crisp_canvas(width, height)

    title_font = load_font(13, font_path=font_path)
    label_font = load_font(12, font_path=font_path)
    stat_font = load_font(12, font_path=font_path)
    detail_font = load_font(9, font_path=font_path)

    left_pad = 7
    right_pad = 7
    top_pad = 3
    bottom_pad = 4
    title_gap = 6
    gap = 9

    title_text = "glm"
    title_bbox = draw.textbbox((0, 0), title_text, font=title_font)
    title_w = title_bbox[2] - title_bbox[0]
    title_h = title_bbox[3] - title_bbox[1]
    draw.text(((width - title_w) // 2, top_pad), title_text, fill=0, font=title_font)

    rows_top = top_pad + title_h + title_gap
    row_count = max(1, len(rows))
    row_height = (height - rows_top - bottom_pad - gap * (row_count - 1)) // row_count

    for idx, row in enumerate(rows):
        row_top = rows_top + idx * (row_height + gap)
        stat_text = row.stat_text or f"{int(round(row.left_percent))}% left"

        label_bbox = draw.textbbox((0, 0), row.label, font=label_font)
        percent_bbox = draw.textbbox((0, 0), stat_text, font=stat_font)
        label_h = label_bbox[3] - label_bbox[1]
        percent_w = percent_bbox[2] - percent_bbox[0]

        draw.text((left_pad, row_top), row.label, fill=0, font=label_font)
        draw.text(
            (width - right_pad - percent_w, row_top),
            stat_text,
            fill=0,
            font=stat_font,
        )

        bar_y = row_top + label_h + 3
        bar_h = 12
        draw_progress_bar(
            draw,
            x=left_pad,
            y=bar_y,
            width=width - left_pad - right_pad - 1,
            height=bar_h,
            percent=row.left_percent,
        )

        detail_bbox = draw.textbbox((0, 0), row.resets_text, font=detail_font)
        detail_w = detail_bbox[2] - detail_bbox[0]
        draw.text(
            (width - right_pad - detail_w, bar_y + bar_h + 4),
            row.resets_text,
            fill=0,
            font=detail_font,
        )

    return img.convert("RGB")


def save_preview(image: Image.Image, output_path: Path) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    image.save(output_path)
    return output_path


# =============================================================================
# BLE 推送 (2.13 inch)
# =============================================================================

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


# =============================================================================
# CLI / Main
# =============================================================================

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="把 GLM Coding Plan usage 画成 2.13 寸电子价签样式并推送。",
    )
    parser.add_argument("--screen", default=DEFAULT_SCREEN, help="屏幕尺寸，默认 2.13inch")
    parser.add_argument("--device", "-d", help="设备名，例如 EDP-F3F4F5F6")
    parser.add_argument("--address", "-a", help="设备 BLE 地址，优先于 --device")
    parser.add_argument("--interval", "-i", type=int, help="包间隔 (ms，默认按屏幕选择)")
    parser.add_argument("--preview-only", action="store_true", help="只生成图片，不推送")
    parser.add_argument("--output", default=DEFAULT_OUTPUT, help=f"预览图输出路径，默认 {DEFAULT_OUTPUT}")
    parser.add_argument("--input-json", type=Path, help="直接读取本地 quota JSON，跳过网络请求")
    parser.add_argument(
        "--settings-path",
        type=Path,
        help="额外加载一层 Claude settings.json（弱 → 强合并）",
    )
    parser.add_argument("--auth-token", help="覆盖 ANTHROPIC_AUTH_TOKEN")
    parser.add_argument(
        "--base-url",
        help="覆盖 monitor 根 URL，例如 https://open.bigmodel.cn",
    )
    parser.add_argument("--timeout", type=float, default=30.0, help="HTTP 超时秒数，默认 30")
    parser.add_argument(
        "--scan-timeout",
        type=float,
        default=DEFAULT_SCAN_TIMEOUT,
        help=f"BLE 单次扫描超时秒数，默认 {DEFAULT_SCAN_TIMEOUT}",
    )
    parser.add_argument(
        "--timezone",
        help="重置时间显示所用时区，默认系统本地时区，例如 Asia/Shanghai",
    )
    parser.add_argument("--font", help="自定义等宽字体路径")
    return parser.parse_args()


def load_usage_payload(args: argparse.Namespace) -> tuple[dict[str, Any], str]:
    if args.input_json:
        try:
            payload = json.loads(args.input_json.read_text(encoding="utf-8"))
        except OSError as exc:
            raise GlmUsageError(f"Failed to read input JSON: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise GlmUsageError(f"Invalid input JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise GlmUsageError("Expected a JSON object in input file.")
        return payload, f"file:{args.input_json}"

    merged = load_merged_claude_env(extra_settings=args.settings_path)
    api_key, root = resolve_credentials(
        merged,
        auth_token=args.auth_token,
        base_url=args.base_url,
    )
    payload = fetch_quota_json(root, api_key, args.timeout)
    return payload, f"{root.rstrip('/')}{QUOTA_PATH}"


def main() -> int:
    args = parse_args()
    profile = get_screen_profile(args.screen)
    if profile.name != "2.13inch":
        print("❌ 当前脚本只为 2.13 寸布局设计，请使用 push_glm_usage_3.7.py", file=sys.stderr)
        return 2

    try:
        payload, source = load_usage_payload(args)
        tzinfo = resolve_timezone(args.timezone)
        rows = build_rows(payload, tzinfo)
        image = render_usage_image(
            rows,
            width=profile.width,
            height=profile.height,
            font_path=args.font,
        )
        output_path = save_preview(image, Path(args.output))
        print(f"预览已保存: {output_path}")
        print(f"Usage 来源: {source}")

        for row in rows:
            stat = row.stat_text or f"{int(round(row.left_percent))}% left"
            print(f"  {row.label}: {stat}, {row.resets_text}")

        if args.preview_only:
            return 0

        try:
            ok = asyncio.run(push_image_to_small_screen(image, args))
        except BleDependencyError as exc:
            print(f"❌ {exc}", file=sys.stderr)
            return 2
        return 0 if ok else 1
    except GlmUsageError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
