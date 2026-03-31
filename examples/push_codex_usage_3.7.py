#!/usr/bin/env python3
"""Render Codex usage in a compact /stats-like layout for 3.7-inch tags (landscape).

默认行为:
1. 从 Codex / ChatGPT OAuth 凭证读取访问令牌
2. 请求 GET {base_url}/wham/usage
3. 生成 416x240 的 usage 面板 (横屏布局)
4. 保存预览图
5. 推送到 3.7 寸设备

示例:
    uv run examples/push_codex_usage_3.7.py --preview-only
    uv run examples/push_codex_usage_3.7.py --device EPD-D984FADA
    uv run examples/push_codex_usage_3.7.py --input-json sample_usage.json --preview-only
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, str(Path(__file__).parent.parent))

from bluetag.ble import BleDependencyError
from bluetag import quantize, pack_2bpp, build_frame, packetize
from bluetag.protocol import parse_mac_suffix
from bluetag.screens import get_screen_profile

DEFAULT_BASE_URL = "https://chatgpt.com/backend-api"
USAGE_PATH = "/wham/usage"
DEFAULT_OUTPUT = "codex-usage-3.7inch.png"
DEFAULT_SCREEN = "3.7inch"
DEFAULT_SCAN_TIMEOUT = 12.0
DEFAULT_SCAN_RETRIES = 3
DEFAULT_CONNECT_RETRIES = 3

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


class CodexUsageError(RuntimeError):
    """Raised when the script cannot load credentials or fetch usage."""


@dataclass
class CodexCredentials:
    access_token: str
    account_id: str | None = None


@dataclass
class UsageRow:
    label: str
    left_percent: float
    resets_text: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="把 Codex usage 画成 3.7 寸电子价签样式并推送 (横屏布局)。",
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
        "--auth-path",
        type=Path,
        help="覆盖 auth.json 路径，默认 CODEX_HOME/auth.json 或 ~/.codex/auth.json",
    )
    parser.add_argument(
        "--config-path",
        type=Path,
        help="覆盖 config.toml 路径，默认 CODEX_HOME/config.toml 或 ~/.codex/config.toml",
    )
    parser.add_argument(
        "--base-url",
        help="覆盖 ChatGPT base URL，可传完整 backend-api 地址",
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


def codex_home_dir() -> Path:
    env_value = os.environ.get("CODEX_HOME", "").strip()
    if env_value:
        return Path(env_value).expanduser()
    return Path.home() / ".codex"


def get_auth_path(override: Path | None) -> Path:
    return (override or (codex_home_dir() / "auth.json")).expanduser()


def get_config_path(override: Path | None) -> Path:
    return (override or (codex_home_dir() / "config.toml")).expanduser()


def load_credentials(auth_path: Path) -> CodexCredentials:
    if not auth_path.exists():
        raise CodexUsageError(
            f"Codex auth.json not found: {auth_path}. Run `codex` and log in first."
        )

    try:
        data = json.loads(auth_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise CodexUsageError(f"Failed to read Codex credentials: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise CodexUsageError(f"Invalid Codex credentials JSON: {exc}") from exc

    api_key = data.get("OPENAI_API_KEY")
    if isinstance(api_key, str) and api_key.strip():
        return CodexCredentials(access_token=api_key.strip())

    tokens = data.get("tokens")
    if not isinstance(tokens, dict):
        raise CodexUsageError("Codex auth.json exists but contains no tokens.")

    access_token = tokens.get("access_token")
    if not isinstance(access_token, str) or not access_token.strip():
        raise CodexUsageError("Missing access_token in Codex credentials.")

    account_id = tokens.get("account_id")
    if isinstance(account_id, str):
        account_id = account_id.strip() or None
    else:
        account_id = None

    return CodexCredentials(access_token=access_token.strip(), account_id=account_id)


def parse_chatgpt_base_url(config_text: str) -> str | None:
    for raw_line in config_text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line or "=" not in line:
            continue

        key, value = line.split("=", 1)
        if key.strip() != "chatgpt_base_url":
            continue

        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        value = value.strip()
        if value:
            return value
    return None


def normalize_base_url(url: str) -> str:
    trimmed = url.strip().rstrip("/")
    if not trimmed:
        return DEFAULT_BASE_URL

    if (
        trimmed.startswith("https://chatgpt.com")
        or trimmed.startswith("https://chat.openai.com")
    ) and "/backend-api" not in trimmed:
        trimmed = f"{trimmed}/backend-api"

    return trimmed


def is_allowed_base_url(url: str) -> bool:
    return (
        url.startswith("https://")
        or url.startswith("http://127.0.0.1")
        or url.startswith("http://localhost")
    )


def resolve_base_url(config_path: Path, override: str | None) -> str:
    if override:
        normalized = normalize_base_url(override)
        if not is_allowed_base_url(normalized):
            raise CodexUsageError(
                f"Insecure base URL rejected: {normalized}. "
                "Use HTTPS, localhost, or 127.0.0.1."
            )
        return normalized

    if config_path.exists():
        try:
            config_text = config_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise CodexUsageError(f"Failed to read config.toml: {exc}") from exc

        base_url = parse_chatgpt_base_url(config_text)
        if base_url:
            normalized = normalize_base_url(base_url)
            if is_allowed_base_url(normalized):
                return normalized

    return DEFAULT_BASE_URL


def fetch_usage_json(
    base_url: str,
    credentials: CodexCredentials,
    timeout: float,
) -> dict[str, Any]:
    url = f"{base_url}{USAGE_PATH}"
    headers = {
        "Authorization": f"Bearer {credentials.access_token}",
        "User-Agent": "push_codex_usage_3.7.py",
        "Accept": "application/json",
    }
    if credentials.account_id:
        headers["ChatGPT-Account-Id"] = credentials.account_id

    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise CodexUsageError(
                "Authentication failed with 401/403. Re-run `codex` to log in again."
            ) from exc
        details = exc.read().decode("utf-8", errors="replace").strip()
        suffix = f": {details}" if details else ""
        raise CodexUsageError(f"Codex API returned HTTP {exc.code}{suffix}") from exc
    except urllib.error.URLError as exc:
        raise CodexUsageError(f"Request failed: {exc.reason}") from exc

    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise CodexUsageError(f"Failed to parse API response as JSON: {exc}") from exc

    if not isinstance(payload, dict):
        raise CodexUsageError("Expected a JSON object from /wham/usage.")
    return payload


def epoch_to_iso(timestamp: Any) -> str | None:
    if not isinstance(timestamp, (int, float)):
        return None
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat().replace(
        "+00:00", "Z"
    )


def parse_used_percent(value: Any) -> float:
    if isinstance(value, bool):
        return 0.0
    if isinstance(value, (int, float)):
        return max(0.0, min(float(value), 100.0))
    return 0.0


def parse_window(window: Any) -> dict[str, Any]:
    if not isinstance(window, dict):
        return {"used_percent": 0.0}

    used_percent = parse_used_percent(
        window.get("used_percent", window.get("usage_percent"))
    )
    limit_window_seconds = window.get("limit_window_seconds")
    window_minutes = None
    if isinstance(limit_window_seconds, (int, float)):
        window_minutes = int(limit_window_seconds) // 60

    result: dict[str, Any] = {"used_percent": used_percent}
    if window_minutes is not None:
        result["window_minutes"] = window_minutes

    resets_at = epoch_to_iso(window.get("reset_at"))
    if resets_at:
        result["resets_at"] = resets_at

    return result


def maybe_parse_window(window: Any) -> dict[str, Any] | None:
    return parse_window(window) if isinstance(window, dict) else None


def extract_rate_limits(
    payload: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    if "usage" in payload and isinstance(payload["usage"], dict):
        usage = payload["usage"]
        primary = usage.get("primary")
        secondary = usage.get("secondary")
        if isinstance(primary, dict):
            return primary, secondary if isinstance(secondary, dict) else None

    rate_limit = payload.get("rate_limit")
    if isinstance(rate_limit, dict):
        primary = maybe_parse_window(rate_limit.get("primary_window")) or {
            "used_percent": 0.0
        }
        secondary = maybe_parse_window(rate_limit.get("secondary_window"))
        return primary, secondary

    rate_limits = payload.get("rate_limits")
    if isinstance(rate_limits, list) and rate_limits:
        primary = parse_window(rate_limits[0])
        secondary = parse_window(rate_limits[1]) if len(rate_limits) > 1 else None
        return primary, secondary

    used_percent = parse_used_percent(
        payload.get("used_percent", payload.get("usage_percent"))
    )
    return {"used_percent": used_percent}, None


def resolve_timezone(name: str | None):
    if not name:
        return datetime.now().astimezone().tzinfo or timezone.utc
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise CodexUsageError(f"Unknown timezone: {name}") from exc


def format_window_label(window_minutes: Any, fallback: str) -> str:
    if not isinstance(window_minutes, int) or window_minutes <= 0:
        return fallback
    if window_minutes == 300:
        return "5h limit"
    if window_minutes == 10080:
        return "weekly limit"
    if window_minutes % 1440 == 0:
        return f"{window_minutes // 1440}d limit"
    if window_minutes % 60 == 0:
        return f"{window_minutes // 60}h limit"
    return f"{window_minutes}m limit"


def format_reset_text(resets_at: Any, tzinfo) -> str:
    if not isinstance(resets_at, str) or not resets_at:
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
    primary, secondary = extract_rate_limits(payload)

    rows = [
        UsageRow(
            label=format_window_label(primary.get("window_minutes"), "5h limit"),
            left_percent=max(
                0.0, min(100.0, 100.0 - parse_used_percent(primary.get("used_percent")))
            ),
            resets_text=format_reset_text(primary.get("resets_at"), tzinfo),
        )
    ]

    if secondary:
        rows.append(
            UsageRow(
                label=format_window_label(
                    secondary.get("window_minutes"), "weekly limit"
                ),
                left_percent=max(
                    0.0,
                    min(
                        100.0,
                        100.0 - parse_used_percent(secondary.get("used_percent")),
                    ),
                ),
                resets_text=format_reset_text(secondary.get("resets_at"), tzinfo),
            )
        )

    return rows[:2]


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

    # Larger fonts for the bigger landscape screen
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

    title_text = "codex"
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

    # Parse MAC suffix and build frame
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
        
        # Send packets
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


def load_usage_payload(args: argparse.Namespace) -> tuple[dict[str, Any], str]:
    if args.input_json:
        try:
            payload = json.loads(args.input_json.read_text(encoding="utf-8"))
        except OSError as exc:
            raise CodexUsageError(f"Failed to read input JSON: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise CodexUsageError(f"Invalid input JSON: {exc}") from exc
        return payload, f"file:{args.input_json}"

    auth_path = get_auth_path(args.auth_path)
    config_path = get_config_path(args.config_path)
    credentials = load_credentials(auth_path)
    base_url = resolve_base_url(config_path, args.base_url)
    payload = fetch_usage_json(base_url, credentials, args.timeout)
    return payload, f"{base_url}{USAGE_PATH}"


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
    except CodexUsageError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
