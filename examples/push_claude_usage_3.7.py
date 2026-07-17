#!/usr/bin/env python3
"""Render Claude Code usage on a 3.7-inch e-ink tag (landscape 416×240).

Shows one row per rate-limit bucket: session (5h), weekly (all models),
and any model-scoped weekly limits (e.g. Fable).

Usage:
    uv run examples/push_claude_usage.py
    uv run examples/push_claude_usage.py --preview-only
    uv run examples/push_claude_usage.py --input-json sample.json --preview-only
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, str(Path(__file__).parent.parent))

from bluetag.ble import BleDependencyError
from bluetag import quantize, pack_2bpp, build_frame, packetize
from bluetag.protocol import parse_mac_suffix
from bluetag.screens import get_screen_profile

USAGE_API = "https://api.anthropic.com/api/oauth/usage"
DEFAULT_OUTPUT = "claude-usage-3.7inch.png"
DEFAULT_SCREEN = "3.7inch"
DEFAULT_SCAN_TIMEOUT = 12.0
DEFAULT_SCAN_RETRIES = 3
DEFAULT_CONNECT_RETRIES = 3
DEFAULT_WATCH_INTERVAL = 60.0

WIDTH = 416
HEIGHT = 240

MONO_FONT_SEARCH = [
    "/System/Library/Fonts/Supplemental/Menlo.ttc",
    "/System/Library/Fonts/Monaco.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    "/usr/share/fonts/TTF/DejaVuSansMono.ttf",
    "C:\\Windows\\Fonts\\consola.ttf",
]

CJK_FONT_SEARCH = [
    str(Path.home() / "Library" / "Fonts" / "fangzhengjuzhenxinfang.ttf"),
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
]


@dataclass
class UsageRow:
    label: str
    left_percent: float
    resets_text: str
    kind: str = ""
    resets_at: str | None = None


# ── OAuth Token ───────────────────────────────────────────────────────────────


def _detect_token_from_keychain() -> str | None:
    raw = subprocess.run(
        ["security", "find-generic-password", "-s", "usage-elink-oauth", "-w"],
        capture_output=True, text=True,
    ).stdout.strip()
    if raw:
        return raw

    raw = subprocess.run(
        ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
        capture_output=True, text=True,
    ).stdout.strip()
    if raw:
        try:
            return json.loads(raw)["claudeAiOauth"]["accessToken"]
        except Exception:
            pass
    return None


def _load_elink_config_token() -> str | None:
    cfg_path = Path.home() / ".config" / "elink" / "config.json"
    if cfg_path.exists():
        try:
            return json.loads(cfg_path.read_text()).get("oauth_token")
        except Exception:
            pass
    return None


def get_oauth_token() -> str | None:
    return _load_elink_config_token() or _detect_token_from_keychain()


# ── API ───────────────────────────────────────────────────────────────────────


def fetch_usage(token: str, timeout: float = 10.0) -> dict[str, Any]:
    req = urllib.request.Request(
        USAGE_API,
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": "oauth-2025-04-20",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"API returned HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Request failed: {exc.reason}") from exc


# ── Data ──────────────────────────────────────────────────────────────────────


def _fmt_resets(iso: str | None) -> str:
    if not iso:
        return "resets unknown"
    try:
        dt = datetime.fromisoformat(iso).astimezone()
        s = (dt - datetime.now(timezone.utc).astimezone()).total_seconds()
        if s <= 0:
            return "reset"
        h, rem = divmod(int(s), 3600)
        m = rem // 60
        if h < 24:
            return f"resets in {h}h {m}m"
        now = datetime.now().astimezone()
        time_text = dt.strftime("%H:%M")
        if dt.date() == now.date():
            return f"resets {time_text}"
        return f"resets {dt.strftime('%a')} {time_text}"
    except (ValueError, TypeError):
        return "resets unknown"


_LIMIT_ORDER = {"session": 0, "weekly_all": 1, "weekly_scoped": 2}


def _scoped_model_name(limit: dict[str, Any]) -> str | None:
    model = (limit.get("scope") or {}).get("model") or {}
    return model.get("display_name") or None


def _label_for_limit(limit: dict[str, Any]) -> str:
    kind = limit.get("kind")
    if kind == "session":
        return "5h"
    if kind == "weekly_all":
        return "weekly"
    if kind == "weekly_scoped":
        return _scoped_model_name(limit) or "scoped"
    return kind or "limit"


def _rows_from_limits(limits: list[dict[str, Any]]) -> list[UsageRow]:
    def sort_key(limit: dict[str, Any]) -> tuple[int, str]:
        return _LIMIT_ORDER.get(limit.get("kind"), 9), _scoped_model_name(limit) or ""

    rows: list[UsageRow] = []
    for limit in sorted(limits, key=sort_key):
        util = limit.get("percent", 0) or 0
        left = max(0.0, min(100.0, 100.0 - util))
        resets_at = limit.get("resets_at")
        rows.append(UsageRow(
            label=_label_for_limit(limit),
            left_percent=left,
            resets_text=_fmt_resets(resets_at),
            kind=limit.get("kind") or "",
            resets_at=resets_at,
        ))
    return rows


def _rows_from_legacy(payload: dict[str, Any]) -> list[UsageRow]:
    rows: list[UsageRow] = []
    for label, kind, key in [("5h", "session", "five_hour"), ("weekly", "weekly_all", "seven_day")]:
        section = payload.get(key) or {}
        util = section.get("utilization", 0) or 0
        left = max(0.0, min(100.0, 100.0 - util))
        resets_at = section.get("resets_at")
        rows.append(UsageRow(
            label=label,
            left_percent=left,
            resets_text=_fmt_resets(resets_at),
            kind=kind,
            resets_at=resets_at,
        ))
    return rows


def build_rows(payload: dict[str, Any]) -> list[UsageRow]:
    limits = payload.get("limits")
    if isinstance(limits, list) and limits:
        return _rows_from_limits(limits)
    return _rows_from_legacy(payload)


# ── Rendering (three columns: label + reset hint + 剩余 NN% per row) ──────────


def load_font(size: int, *, font_path: str | None = None) -> ImageFont.FreeTypeFont:
    if font_path:
        return ImageFont.truetype(font_path, size)
    for path in MONO_FONT_SEARCH:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def load_cjk_font(size: int, *, font_path: str | None = None) -> ImageFont.FreeTypeFont:
    candidates = ([font_path] if font_path else []) + CJK_FONT_SEARCH
    for path in candidates:
        if not path:
            continue
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return load_font(size)


COLOR_RED = (255, 0, 0)
COLOR_YELLOW = (255, 255, 0)


def _bar_fill_color(left_percent: float) -> tuple[int, int, int]:
    """Pick bar fill color based on remaining usage."""
    if left_percent <= 20:
        return COLOR_RED
    if left_percent <= 50:
        return COLOR_YELLOW
    return (0, 0, 0)  # black


_CN_WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def _reset_cn(iso: str | None) -> str:
    """Chinese reset hint: '3h后' within a day, else '周三 21:00'."""
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(iso).astimezone()
        secs = (dt - datetime.now(timezone.utc).astimezone()).total_seconds()
    except (ValueError, TypeError):
        return ""
    if secs <= 0:
        return "已重置"
    if secs < 24 * 3600:
        h = int(secs) // 3600
        if h >= 1:
            return f"{h}h后"
        return f"{max(1, int(secs) // 60)}m后"
    return f"{_CN_WEEKDAYS[dt.weekday()]} {dt.strftime('%H:%M')}"


def render_usage_image(
    rows: list[UsageRow],
    *,
    width: int = WIDTH,
    height: int = HEIGHT,
    font_path: str | None = None,
) -> Image.Image:
    """Render usage on the 3.7 inch landscape tag (416x240), text-forward.

    Three columns per limit — short label (left), Chinese reset hint (middle),
    "剩余 NN%" (right). The reset column fills the former mid-row whitespace,
    so no separate footer is needed. Chinese uses a CJK font, numbers stay mono.
    """
    img = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(img)

    row_count = max(1, len(rows))
    left_pad = 22
    right_pad = 22
    top_pad = 14
    bottom_pad = 16

    title_font = load_font(26, font_path=font_path)

    title_text = "CC Usage"
    tb = draw.textbbox((0, 0), title_text, font=title_font)
    title_w = tb[2] - tb[0]
    title_h = tb[3] - tb[1]
    tx = (width - title_w) // 2
    for dx in (-1, 0, 1):
        for dy in (-1, 0, 1):
            draw.text((tx + dx, top_pad + dy), title_text, fill=COLOR_RED, font=title_font)

    band_top = top_pad + title_h + 20
    band_bottom = height - bottom_pad
    row_h = max(1, (band_bottom - band_top) // row_count)

    label_font = load_font(min(28, max(18, row_h - 20)), font_path=font_path)
    pct_size = min(34, max(22, row_h - 16))
    pct_font = load_font(pct_size, font_path=font_path)
    prefix_font = load_cjk_font(max(18, pct_size - 8))
    reset_font = load_cjk_font(max(15, pct_size - 14))

    x_right = width - right_pad
    prefix = "剩余"
    prefix_gap = 8
    reset_gap = 18
    prefix_w = draw.textlength(prefix, font=prefix_font)

    for idx, row in enumerate(rows):
        mid = band_top + idx * row_h + row_h // 2
        color = _bar_fill_color(row.left_percent)

        label = row.label if len(row.label) <= 8 else row.label[:7] + "…"
        draw.text((left_pad, mid), label, fill="black", font=label_font, anchor="lm")

        pct_text = f"{int(round(row.left_percent))}%"
        num_w = draw.textlength(pct_text, font=pct_font)
        draw.text((x_right, mid), pct_text, fill=color, font=pct_font, anchor="rm")

        prefix_right = x_right - num_w - prefix_gap
        draw.text((prefix_right, mid), prefix, fill="black", font=prefix_font, anchor="rm")

        reset_text = _reset_cn(row.resets_at)
        if reset_text:
            reset_right = prefix_right - prefix_w - reset_gap
            draw.text((reset_right, mid), reset_text, fill="black", font=reset_font, anchor="rm")

    return img


# ── BLE push ──────────────────────────────────────────────────────────────────


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
    search_address = getattr(args, "address", None)
    if not search_name and not search_address:
        cached = _load_device(profile)
        if cached:
            print(
                f"使用 {profile.name} 缓存设备: "
                f"{cached['name']} ({cached['address']})"
            )
            search_name = cached["name"]
            search_address = cached["address"]

    print(f"扫描 {profile.name} 设备 ({profile.device_prefix}*, {args.scan_timeout:.1f}s/次)...")
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

    native = image.transpose(Image.Transpose.ROTATE_270)
    if native.size != profile.size:
        native = native.resize(profile.size, Image.LANCZOS)
    return native


async def _send_packets(session, packets: list[bytes], interval_ms: int) -> bool:
    try:
        total = len(packets)
        for index, packet in enumerate(packets, start=1):
            await session.write(packet, response=False)
            await asyncio.sleep(interval_ms / 1000.0)
            _on_progress(index, total)
        return True
    except Exception as exc:
        print(f"\n❌ 发送失败: {exc}")
        return False


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
        return await _send_packets(session, packets, interval_ms)
    finally:
        await session.close()


# ── CLI ───────────────────────────────────────────────────────────────────────


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Push Claude Code usage to 3.7-inch e-ink tag (landscape).",
    )
    parser.add_argument("--screen", default=DEFAULT_SCREEN)
    parser.add_argument("--device", "-d", help="Device name e.g. EPD-7F1C654B")
    parser.add_argument("--address", "-a", help="Device BLE address")
    parser.add_argument("--interval", "-i", type=int, help="Packet interval (ms)")
    parser.add_argument("--preview-only", action="store_true")
    parser.add_argument("--output", default=DEFAULT_OUTPUT)
    parser.add_argument("--input-json", type=Path, help="Read usage from local JSON")
    parser.add_argument("--scan-timeout", type=float, default=DEFAULT_SCAN_TIMEOUT)
    parser.add_argument("--font", help="Custom monospace font path")
    parser.add_argument(
        "--watch", type=float, nargs="?", const=DEFAULT_WATCH_INTERVAL,
        metavar="SECONDS",
        help="Watch --input-json for changes and re-push (default: 60s)",
    )
    return parser.parse_args()


def _run_once(args) -> int:
    if args.input_json:
        payload = json.loads(args.input_json.read_text())
        source = f"file:{args.input_json}"
    else:
        token = get_oauth_token()
        if not token:
            print("❌ No OAuth token found.", file=sys.stderr)
            print("   Run: claude auth login", file=sys.stderr)
            return 1
        payload = fetch_usage(token)
        source = USAGE_API

    rows = build_rows(payload)
    image = render_usage_image(rows, font_path=args.font)
    image.save(args.output)
    print(f"预览已保存: {args.output}")
    print(f"数据来源: {source}")
    for row in rows:
        print(f"  {row.label}: {int(round(row.left_percent))}% left, {row.resets_text}")

    if args.preview_only:
        return 0

    try:
        ok = asyncio.run(push_image_to_37_screen(image, args))
    except BleDependencyError as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return 1
    return 0 if ok else 1


async def _watch_loop(args) -> int:
    from bluetag.ble import connect_session

    profile = get_screen_profile(args.screen)
    interval_ms = args.interval or profile.default_interval_ms

    target = await _find_target(args, profile)
    if not target:
        print("❌ 未找到设备")
        return 1

    mac_suffix = parse_mac_suffix(target["name"])

    session = await connect_session(
        target.get("_ble_device") or target["address"],
        timeout=20.0,
        connect_retries=DEFAULT_CONNECT_RETRIES,
    )
    if not session:
        print("❌ 连接设备失败")
        return 1

    print(f"🔗 已连接 {target['name']} [{profile.name}]，保持连接")

    watch_interval = args.watch
    last_mtime = 0.0
    last_2bpp: bytes | None = None

    print(f"👀 Watching {args.input_json} every {watch_interval:.0f}s ...")

    try:
        while True:
            try:
                mtime = args.input_json.stat().st_mtime
            except FileNotFoundError:
                await asyncio.sleep(watch_interval)
                continue

            if mtime == last_mtime:
                await asyncio.sleep(watch_interval)
                continue

            last_mtime = mtime
            ts = datetime.now().strftime("%H:%M:%S")

            payload = json.loads(args.input_json.read_text())
            rows = build_rows(payload)
            image = render_usage_image(rows, font_path=args.font)
            image.save(args.output)

            native_img = prepare_landscape_image_for_37_screen(image, profile)
            indices = quantize(native_img, flip=profile.mirror, size=profile.size)
            data_2bpp = pack_2bpp(indices)

            if data_2bpp == last_2bpp:
                print(f"[{ts}] File changed, image identical — skipped")
                await asyncio.sleep(watch_interval)
                continue

            last_2bpp = data_2bpp

            frame = build_frame(mac_suffix, data_2bpp)
            packets = packetize(frame)

            print(f"\n[{ts}] File changed, pushing {len(packets)} packets...")
            for row in rows:
                print(f"  {row.label}: {int(round(row.left_percent))}% left, {row.resets_text}")

            ok = await _send_packets(session, packets, interval_ms)
            if not ok:
                print("⚡ 连接断开，重连中...")
                await session.close()
                session = await connect_session(
                    target.get("_ble_device") or target["address"],
                    timeout=20.0,
                    connect_retries=DEFAULT_CONNECT_RETRIES,
                )
                if not session:
                    print("❌ 重连失败")
                    return 1
                print("🔗 重连成功，重试发送...")
                ok = await _send_packets(session, packets, interval_ms)
                if not ok:
                    print("❌ 重试失败")
                    return 1

            await asyncio.sleep(watch_interval)
    except KeyboardInterrupt:
        print("\n👋 Stopped.")
        return 0
    finally:
        await session.close()


def main() -> int:
    args = parse_args()

    if not args.watch:
        return _run_once(args)

    if not args.input_json:
        print("❌ --watch requires --input-json", file=sys.stderr)
        return 1

    try:
        return asyncio.run(_watch_loop(args))
    except BleDependencyError as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
