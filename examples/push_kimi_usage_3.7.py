#!/usr/bin/env python3
"""Render Kimi Code CLI usage in a compact /stats-like layout for 3.7-inch tags (landscape).

默认行为:
1. 从 Kimi CLI config/credentials 读取访问令牌
2. 请求 GET {base_url}/usages
3. 生成 416x240 的 usage 面板 (横屏布局)
4. 保存预览图
5. 推送到 3.7 寸设备

示例:
    uv run examples/push_kimi_usage_3.7.py --preview-only
    uv run examples/push_kimi_usage_3.7.py --device EPD-D984FADA
    uv run examples/push_kimi_usage_3.7.py --input-json sample_usage.json --preview-only
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, NamedTuple, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, str(Path(__file__).parent.parent))

from bluetag.ble import BleDependencyError
from bluetag import quantize, pack_2bpp, build_frame, packetize
from bluetag.protocol import parse_mac_suffix
from bluetag.screens import get_screen_profile

DEFAULT_BASE_URL = "https://api.kimi.com/coding/v1"
USAGE_PATH = "/usages"
DEFAULT_OUTPUT = "kimi-usage-3.7inch.png"
DEFAULT_SCREEN = "3.7inch"
DEFAULT_SCAN_TIMEOUT = 12.0
DEFAULT_SCAN_RETRIES = 3
DEFAULT_CONNECT_RETRIES = 3

KIMI_CODE_PLATFORM_ID = "kimi-code"
MANAGED_PROVIDER_PREFIX = "managed:"
DEFAULT_CONFIG_PATH = Path.home() / ".kimi" / "config.toml"

# 3.7 inch landscape dimensions
WIDTH = 416
HEIGHT = 240

MONO_FONT_SEARCH = [
    "/System/Library/Fonts/Supplemental/Menlo.ttc",
    "/System/Library/Fonts/Monaco.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    "/usr/share/fonts/TTF/DejaVuSansMono.ttf",
    "C:\\Windows\\Fonts\\consola.ttf",
]


class KimiUsageError(RuntimeError):
    """Raised when the script cannot load credentials or fetch usage."""


class Platform(NamedTuple):
    id: str
    name: str
    base_url: str


@dataclass
class UsageRow:
    label: str
    left_percent: float
    resets_text: str


# =============================================================================
# 配置相关
# =============================================================================

def _kimi_code_base_url() -> str:
    if base_url := os.getenv("KIMI_CODE_BASE_URL"):
        return base_url
    return DEFAULT_BASE_URL


PLATFORMS: list[Platform] = [
    Platform(
        id=KIMI_CODE_PLATFORM_ID,
        name="Kimi Code",
        base_url=_kimi_code_base_url(),
    ),
]

_PLATFORM_BY_ID = {platform.id: platform for platform in PLATFORMS}


def get_platform_by_id(platform_id: str) -> Platform | None:
    return _PLATFORM_BY_ID.get(platform_id)


def managed_provider_key(platform_id: str) -> str:
    return f"{MANAGED_PROVIDER_PREFIX}{platform_id}"


def parse_managed_provider_key(provider: str) -> str | None:
    if provider.startswith(MANAGED_PROVIDER_PREFIX):
        return provider[len(MANAGED_PROVIDER_PREFIX):]
    return None


# =============================================================================
# 工具函数
# =============================================================================

def format_duration(seconds: int) -> str:
    delta = timedelta(seconds=seconds)
    parts: list[str] = []
    days = delta.days
    if days:
        parts.append(f"{days}d")
    hours, remainder = divmod(delta.seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if secs and not parts:
        parts.append(f"{secs}s")
    return " ".join(parts) or "0s"


def _to_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# =============================================================================
# 解析函数
# =============================================================================

def _format_reset_time(val: str) -> str:
    try:
        if "." in val and val.endswith("Z"):
            base, frac = val[:-1].split(".")
            frac = frac[:6]
            val = f"{base}.{frac}Z"
        dt = datetime.fromisoformat(val.replace("Z", "+00:00"))
        now = datetime.now(UTC)
        delta = dt - now

        if delta.total_seconds() <= 0:
            return "reset"
        return f"resets in {format_duration(int(delta.total_seconds()))}"
    except (ValueError, TypeError):
        return f"resets at {val}"


def _reset_hint(data: Mapping[str, Any]) -> str | None:
    for key in ("reset_at", "resetAt", "reset_time", "resetTime"):
        if val := data.get(key):
            return _format_reset_time(str(val))

    for key in ("reset_in", "resetIn", "ttl", "window"):
        seconds = _to_int(data.get(key))
        if seconds:
            return f"resets in {format_duration(seconds)}"

    return None


@dataclass(slots=True, frozen=True)
class _ParsedUsageRow:
    label: str
    used: int
    limit: int
    reset_hint: str | None = None


def _to_usage_row(data: Mapping[str, Any], *, default_label: str) -> _ParsedUsageRow | None:
    limit = _to_int(data.get("limit"))
    used = _to_int(data.get("used"))
    if used is None:
        remaining = _to_int(data.get("remaining"))
        if remaining is not None and limit is not None:
            used = limit - remaining
    if used is None and limit is None:
        return None
    return _ParsedUsageRow(
        label=str(data.get("name") or data.get("title") or default_label),
        used=used or 0,
        limit=limit or 0,
        reset_hint=_reset_hint(data),
    )


def _limit_label(
    item: Mapping[str, Any],
    detail: Mapping[str, Any],
    window: Mapping[str, Any],
    idx: int,
) -> str:
    for key in ("name", "title", "scope"):
        if val := (item.get(key) or detail.get(key)):
            return str(val)

    duration = _to_int(window.get("duration") or item.get("duration") or detail.get("duration"))
    time_unit = window.get("timeUnit") or item.get("timeUnit") or detail.get("timeUnit") or ""
    if duration:
        if "MINUTE" in time_unit:
            if duration >= 60 and duration % 60 == 0:
                return f"{duration // 60}h limit"
            return f"{duration}m limit"
        if "HOUR" in time_unit:
            return f"{duration}h limit"
        if "DAY" in time_unit:
            return f"{duration}d limit"
        return f"{duration}s limit"

    return f"Limit #{idx + 1}"


def _parse_usage_payload(
    payload: Mapping[str, Any],
) -> tuple[_ParsedUsageRow | None, list[_ParsedUsageRow]]:
    summary: _ParsedUsageRow | None = None
    limits: list[_ParsedUsageRow] = []

    usage = payload.get("usage")
    if isinstance(usage, Mapping):
        usage_map: Mapping[str, Any] = cast(Mapping[str, Any], usage)
        summary = _to_usage_row(usage_map, default_label="Weekly limit")

    raw_limits_obj = payload.get("limits")
    if isinstance(raw_limits_obj, Sequence):
        limits_seq: Sequence[Any] = cast(Sequence[Any], raw_limits_obj)
        for idx, item in enumerate(limits_seq):
            if not isinstance(item, Mapping):
                continue
            item_map: Mapping[str, Any] = cast(Mapping[str, Any], item)
            detail_raw = item_map.get("detail")
            detail: Mapping[str, Any] = (
                cast(Mapping[str, Any], detail_raw) if isinstance(detail_raw, Mapping) else item_map
            )
            window_raw = item_map.get("window")
            window: Mapping[str, Any] = (
                cast(Mapping[str, Any], window_raw) if isinstance(window_raw, Mapping) else {}
            )
            label = _limit_label(item_map, detail, window, idx)
            row = _to_usage_row(detail, default_label=label)
            if row:
                limits.append(row)

    return summary, limits


# =============================================================================
# API 请求 / 配置加载
# =============================================================================

def _load_oauth_token(oauth_key: str) -> str | None:
    key_name = oauth_key.removeprefix("oauth/").split("/")[-1] or oauth_key
    credentials_file = Path.home() / ".kimi" / "credentials" / f"{key_name}.json"

    if not credentials_file.exists():
        return None

    try:
        with open(credentials_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("access_token")
    except (json.JSONDecodeError, OSError):
        return None


def load_api_key_from_config(config_path: Path | None = None) -> tuple[str, str] | None:
    if config_path is None:
        config_path = DEFAULT_CONFIG_PATH

    if not config_path.exists():
        return None

    try:
        import tomli
        with open(config_path, "rb") as f:
            config = tomli.load(f)
    except Exception as e:
        print(f"Error reading config: {e}", file=sys.stderr)
        return None

    providers = config.get("providers", {})
    kimi_provider_key = managed_provider_key(KIMI_CODE_PLATFORM_ID)

    provider = providers.get(kimi_provider_key)
    if provider is None:
        for key, p in providers.items():
            if key.startswith(MANAGED_PROVIDER_PREFIX):
                provider = p
                break

    if provider is None:
        print("No managed provider found in config", file=sys.stderr)
        return None

    api_key = None
    oauth = provider.get("oauth")
    if oauth and oauth.get("storage") == "file":
        oauth_key = oauth.get("key")
        if oauth_key:
            api_key = _load_oauth_token(oauth_key)

    if not api_key:
        api_key = provider.get("api_key")

    if not api_key:
        print("No API key found", file=sys.stderr)
        return None

    base_url = provider.get("base_url")
    if base_url is None:
        platform = get_platform_by_id(KIMI_CODE_PLATFORM_ID)
        if platform:
            base_url = platform.base_url
        else:
            base_url = _kimi_code_base_url()

    return (str(api_key), str(base_url))


def fetch_usage_json(
    base_url: str,
    api_key: str,
    timeout: float,
) -> dict[str, Any]:
    url = f"{base_url.rstrip('/')}{USAGE_PATH}"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "User-Agent": "push_kimi_usage_3.7.py",
        "Accept": "application/json",
    }

    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise KimiUsageError(
                "Authentication failed with 401/403. Please check your Kimi CLI login."
            ) from exc
        details = exc.read().decode("utf-8", errors="replace").strip()
        suffix = f": {details}" if details else ""
        raise KimiUsageError(f"Kimi API returned HTTP {exc.code}{suffix}") from exc
    except urllib.error.URLError as exc:
        raise KimiUsageError(f"Request failed: {exc.reason}") from exc

    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise KimiUsageError(f"Failed to parse API response as JSON: {exc}") from exc

    if not isinstance(payload, dict):
        raise KimiUsageError("Expected a JSON object from /usages.")
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
        raise KimiUsageError(f"Unknown timezone: {name}") from exc


def _format_reset_text_from_hint(hint: str | None, tzinfo) -> str:
    if not hint:
        return "resets unknown"

    if hint == "reset":
        return "reset"

    if hint.startswith("resets at "):
        iso_value = hint[len("resets at "):].replace("Z", "+00:00")
        try:
            reset_dt = datetime.fromisoformat(iso_value).astimezone(tzinfo)
        except ValueError:
            return hint
        now_dt = datetime.now(tzinfo)
        time_text = reset_dt.strftime("%H:%M")
        if reset_dt.date() == now_dt.date():
            return f"resets {time_text}"
        if reset_dt.year == now_dt.year:
            return f"resets {time_text} on {reset_dt.day} {reset_dt.strftime('%b')}"
        return f"resets {time_text} on {reset_dt:%Y-%m-%d}"

    return hint


def build_rows(payload: dict[str, Any], tzinfo) -> list[UsageRow]:
    summary, limits = _parse_usage_payload(payload)
    rows: list[UsageRow] = []

    sources = []
    if summary:
        sources.append(summary)
    sources.extend(limits)

    for src in sources[:2]:
        limit = src.limit
        used = src.used
        left_percent = 0.0
        if limit > 0:
            left_percent = max(0.0, min(100.0, (limit - used) / limit * 100.0))
        rows.append(
            UsageRow(
                label=src.label,
                left_percent=left_percent,
                resets_text=_format_reset_text_from_hint(src.reset_hint, tzinfo),
            )
        )

    return rows


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


def draw_progress_bar(
    draw: ImageDraw.ImageDraw,
    *,
    x: int,
    y: int,
    width: int,
    height: int,
    percent: float,
):
    draw.rectangle((x, y, x + width, y + height), outline="black", width=2)
    inner_x0 = x + 3
    inner_y0 = y + 3
    inner_x1 = x + width - 2
    inner_y1 = y + height - 2
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
    width: int = WIDTH,
    height: int = HEIGHT,
    font_path: str | None = None,
) -> Image.Image:
    """Render usage image for 3.7 inch landscape layout (416x240)."""
    img = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(img)

    title_font = load_font(24, font_path=font_path)
    label_font = load_font(20, font_path=font_path)
    stat_font = load_font(22, font_path=font_path)
    detail_font = load_font(14, font_path=font_path)

    left_pad = 20
    right_pad = 20
    top_pad = 15
    bottom_pad = 15
    title_gap = 15
    gap = 25

    title_text = "kimi"
    title_bbox = draw.textbbox((0, 0), title_text, font=title_font)
    title_w = title_bbox[2] - title_bbox[0]
    title_h = title_bbox[3] - title_bbox[1]
    draw.text(
        ((width - title_w) // 2, top_pad),
        title_text,
        fill="black",
        font=title_font,
    )

    rows_top = top_pad + title_h + title_gap
    row_count = max(1, len(rows))
    row_height = (height - rows_top - bottom_pad - gap * (row_count - 1)) // row_count

    for idx, row in enumerate(rows):
        row_top = rows_top + idx * (row_height + gap)
        percent_text = f"{int(round(row.left_percent))}% left"

        label_bbox = draw.textbbox((0, 0), row.label, font=label_font)
        percent_bbox = draw.textbbox((0, 0), percent_text, font=stat_font)
        label_h = label_bbox[3] - label_bbox[1]
        percent_w = percent_bbox[2] - percent_bbox[0]

        draw.text((left_pad, row_top), row.label, fill="black", font=label_font)
        draw.text(
            (width - right_pad - percent_w, row_top - 2),
            percent_text,
            fill="black",
            font=stat_font,
        )

        bar_y = row_top + label_h + 8
        bar_h = 20
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
            (width - right_pad - detail_w, bar_y + bar_h + 8),
            row.resets_text,
            fill="black",
            font=detail_font,
        )

    return img


def save_preview(image: Image.Image, output_path: Path) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    image.save(output_path)
    return output_path


# =============================================================================
# BLE 推送 (3.7 inch)
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


def _on_progress(sent: int, total: int):
    if sent == total:
        print(f"\r✅ 发送完成! ({total} 包)")
    elif sent == 1 or sent % 10 == 0:
        print(f"\r  发送中 {sent}/{total}...", end="", flush=True)


def prepare_landscape_image_for_37_screen(
    image: Image.Image,
    profile,
) -> Image.Image:
    """Rotate the landscape preview into the panel's native portrait buffer."""
    if image.size != (WIDTH, HEIGHT):
        image = image.convert("RGB").resize((WIDTH, HEIGHT), Image.LANCZOS)
    else:
        image = image.convert("RGB")

    native = image.transpose(Image.Transpose.ROTATE_90)
    if native.size != profile.size:
        native = native.resize(profile.size, Image.LANCZOS)
    return native


async def push_image_to_37_screen(image: Image.Image, args) -> bool:
    from bluetag.ble import connect_session, push

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


# =============================================================================
# CLI / Main
# =============================================================================

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="把 Kimi Code usage 画成 3.7 寸电子价签样式并推送 (横屏布局)。",
    )
    parser.add_argument(
        "--screen",
        default=DEFAULT_SCREEN,
        help="屏幕尺寸，默认 3.7inch",
    )
    parser.add_argument(
        "--device",
        "-d",
        help="设备名，例如 EPD-D984FADA",
    )
    parser.add_argument(
        "--address",
        "-a",
        help="设备 BLE 地址，优先于 --device",
    )
    parser.add_argument(
        "--interval",
        "-i",
        type=int,
        help="包间隔 (ms，默认按屏幕选择)",
    )
    parser.add_argument(
        "--preview-only",
        action="store_true",
        help="只生成图片，不推送",
    )
    parser.add_argument(
        "--output",
        default=DEFAULT_OUTPUT,
        help=f"预览图输出路径，默认 {DEFAULT_OUTPUT}",
    )
    parser.add_argument(
        "--input-json",
        type=Path,
        help="直接读取本地 usage JSON，跳过网络请求",
    )
    parser.add_argument(
        "--config-path",
        type=Path,
        help=f"覆盖 config.toml 路径，默认 {DEFAULT_CONFIG_PATH}",
    )
    parser.add_argument(
        "--base-url",
        help=f"覆盖 base URL，默认 {_kimi_code_base_url()}",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        help="HTTP 超时秒数，默认 30",
    )
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
    parser.add_argument(
        "--font",
        help="自定义等宽字体路径",
    )
    return parser.parse_args()


def load_usage_payload(args: argparse.Namespace) -> tuple[dict[str, Any], str]:
    if args.input_json:
        try:
            payload = json.loads(args.input_json.read_text(encoding="utf-8"))
        except OSError as exc:
            raise KimiUsageError(f"Failed to read input JSON: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise KimiUsageError(f"Invalid input JSON: {exc}") from exc
        return payload, f"file:{args.input_json}"

    result = load_api_key_from_config(args.config_path)
    if result is None:
        raise KimiUsageError(
            "Failed to load API key from config. "
            f"Please ensure your config is set up correctly at {DEFAULT_CONFIG_PATH}."
        )
    api_key, base_url = result
    if args.base_url:
        base_url = args.base_url

    payload = fetch_usage_json(base_url, api_key, args.timeout)
    return payload, f"{base_url.rstrip('/')}{USAGE_PATH}"


def main() -> int:
    args = parse_args()
    profile = get_screen_profile(args.screen)
    if profile.name != "3.7inch":
        print("❌ 当前脚本只为 3.7 寸横屏布局设计，请使用 --screen 3.7inch", file=sys.stderr)
        return 2

    try:
        payload, source = load_usage_payload(args)
        tzinfo = resolve_timezone(args.timezone)
        rows = build_rows(payload, tzinfo)
        image = render_usage_image(
            rows,
            width=WIDTH,
            height=HEIGHT,
            font_path=args.font,
        )
        output_path = save_preview(image, Path(args.output))
        print(f"预览已保存: {output_path}")
        print(f"Usage 来源: {source}")

        for row in rows:
            print(f"  {row.label}: {int(round(row.left_percent))}% left, {row.resets_text}")

        if args.preview_only:
            return 0

        try:
            ok = asyncio.run(push_image_to_37_screen(image, args))
        except BleDependencyError as exc:
            print(f"❌ {exc}", file=sys.stderr)
            return 2
        return 0 if ok else 1
    except KimiUsageError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
