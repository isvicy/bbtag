#!/usr/bin/env python3
"""Fuzz EPD frame header bytes to find faster e-ink refresh modes.

Sends mutated frames over a persistent BLE connection, one at a time.
The operator observes the display after each send and decides how to proceed.

Usage:
    uv run examples/fuzz_refresh_mode.py -d EPD-E1FBDFD8
    uv run examples/fuzz_refresh_mode.py -d EPD-E1FBDFD8 --dry-run
    uv run examples/fuzz_refresh_mode.py -d EPD-E1FBDFD8 --start-from B3
    uv run examples/fuzz_refresh_mode.py --preview-only
"""

from __future__ import annotations

import argparse
import asyncio
import struct
import sys
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).parent.parent))

from bluetag import quantize, pack_2bpp, build_frame, packetize
from bluetag.protocol import parse_mac_suffix
from bluetag.screens import get_screen_profile

SCREEN = "3.7inch"
DEFAULT_INTERVAL_MS = 50
DEFAULT_DELAY = 8.0
SCAN_TIMEOUT = 12.0
CONNECT_RETRIES = 3


# ── Mutations ────────────────────────────────────────────────────────────────


@dataclass
class Mutation:
    id: str
    group: str
    description: str
    patches: list[tuple[int, bytes]] = field(default_factory=list)


MUTATIONS: list[Mutation] = [
    # Group A: byte 12 (default 0x02) — possible display mode flag
    Mutation("A1", "A", "byte12=0x00 (B/W mode?)", [(12, b"\x00")]),
    Mutation("A2", "A", "byte12=0x01 (reduced color?)", [(12, b"\x01")]),
    Mutation("A3", "A", "byte12=0x03 (adjacent enum?)", [(12, b"\x03")]),
    Mutation("A4", "A", "byte12=0x04 (partial refresh?)", [(12, b"\x04")]),
    Mutation("A5", "A", "byte12=0x06 (bitfield check)", [(12, b"\x06")]),
    Mutation("A6", "A", "byte12=0x08 (bitfield check)", [(12, b"\x08")]),
    # Group B: bytes 17-24 (default 00,04 x4) — per-color-plane waveform params
    Mutation("B1", "B", "planes=all zeros", [(17, b"\x00\x00\x00\x00\x00\x00\x00\x00")]),
    Mutation("B2", "B", "planes=00,01 x4 (minimal)", [(17, b"\x00\x01\x00\x01\x00\x01\x00\x01")]),
    Mutation("B3", "B", "planes=00,02 x4", [(17, b"\x00\x02\x00\x02\x00\x02\x00\x02")]),
    Mutation("B4", "B", "planes=BW zeroed, YR normal", [(17, b"\x00\x00\x00\x00\x00\x04\x00\x04")]),
    Mutation("B5", "B", "planes=BW normal, YR zeroed", [(17, b"\x00\x04\x00\x04\x00\x00\x00\x00")]),
    Mutation("B6", "B", "plane0 only zeroed (17-18)", [(17, b"\x00\x00")]),
    Mutation("B7", "B", "plane1 only zeroed (19-20)", [(19, b"\x00\x00")]),
    Mutation("B8", "B", "planes 0-1 = 00,01 (17-20)", [(17, b"\x00\x01\x00\x01")]),
    # Group C: byte 10 (default 0x0C) — command code
    Mutation("C1", "C", "byte10=0x00 (null cmd)", [(10, b"\x00")]),
    Mutation("C2", "C", "byte10=0x04 (partial cmd?)", [(10, b"\x04")]),
    Mutation("C3", "C", "byte10=0x08 (bitfield)", [(10, b"\x08")]),
    # Group D: combinations
    Mutation("D1", "D", "byte12=0x00 + planes=all zeros",
             [(12, b"\x00"), (17, b"\x00\x00\x00\x00\x00\x00\x00\x00")]),
    Mutation("D2", "D", "byte12=0x01 + planes=00,01 x4",
             [(12, b"\x01"), (17, b"\x00\x01\x00\x01\x00\x01\x00\x01")]),
]


# ── Helpers ──────────────────────────────────────────────────────────────────


def generate_test_image() -> Image.Image:
    """Half-black (top), half-white (bottom) — easy to visually verify."""
    profile = get_screen_profile(SCREEN)
    w, h = profile.size  # 240x416 native (portrait)
    img = Image.new("RGB", (w, h), "white")
    draw = ImageDraw.Draw(img)
    draw.rectangle([0, 0, w, h // 2], fill="black")
    return img


def recalculate_session_checksum(frame: bytearray) -> None:
    """Recalculate session checksum (bytes 8-9) from image_part (bytes 10+)."""
    session_val = sum(frame[10:]) & 0xFFFF
    struct.pack_into(">H", frame, 8, session_val)


def apply_mutation(baseline: bytes, mutation: Mutation) -> bytearray:
    """Clone baseline, apply patches, fix checksum."""
    frame = bytearray(baseline)
    for offset, data in mutation.patches:
        frame[offset : offset + len(data)] = data
    recalculate_session_checksum(frame)
    return frame


def format_header(frame: bytes | bytearray, n: int = 30) -> str:
    """Hex dump of the first n bytes."""
    return " ".join(f"{b:02x}" for b in frame[:n])


def print_mutation_info(mutation: Mutation, baseline: bytes, mutated: bytearray):
    """Print mutation details and hex diff."""
    print(f"\n{'='*60}")
    print(f"  [{mutation.id}] {mutation.description}")
    print(f"{'='*60}")
    for offset, data in mutation.patches:
        orig = baseline[offset : offset + len(data)]
        print(f"  offset {offset:2d}: {orig.hex(' ')} → {data.hex(' ')}")
    print(f"  baseline header: {format_header(baseline)}")
    print(f"  mutated  header: {format_header(mutated)}")
    cksum_b = struct.unpack_from(">H", baseline, 8)[0]
    cksum_m = struct.unpack_from(">H", mutated, 8)[0]
    if cksum_b != cksum_m:
        print(f"  checksum: {cksum_b:#06x} → {cksum_m:#06x}")


# ── BLE send ─────────────────────────────────────────────────────────────────


async def send_frame(session, frame: bytes, interval_ms: int) -> bool:
    """Packetize and send a frame through an existing BLE session."""
    packets = packetize(frame)
    try:
        total = len(packets)
        for i, pkt in enumerate(packets, 1):
            await session.write(pkt, response=False)
            await asyncio.sleep(interval_ms / 1000.0)
            if i == total:
                print(f"\r  sent {total}/{total} packets", flush=True)
            elif i == 1 or i % 20 == 0:
                print(f"\r  sending {i}/{total}...", end="", flush=True)
        return True
    except Exception as exc:
        print(f"\n  send failed: {exc}")
        return False


async def async_input(prompt: str) -> str:
    """Non-blocking input that keeps the event loop alive."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, input, prompt)


# ── Main ─────────────────────────────────────────────────────────────────────


async def run(args):
    from bluetag.ble import find_device, connect_session

    profile = get_screen_profile(SCREEN)

    # Generate test image
    img = generate_test_image()
    indices = quantize(img, flip=profile.mirror, size=profile.size)
    data_2bpp = pack_2bpp(indices)

    # We need a mac_suffix for build_frame. For preview/dry-run, use a dummy.
    if args.preview_only or args.dry_run:
        mac_suffix = b"\x00\x00\x00\x00"
    else:
        mac_suffix = parse_mac_suffix(args.device)

    baseline = build_frame(mac_suffix, data_2bpp)

    print(f"Test image: {profile.size[0]}x{profile.size[1]} half-black/half-white")
    print(f"Frame: {len(baseline)} bytes, {len(packetize(baseline))} packets")
    print(f"Header (first 30 bytes): {format_header(baseline)}")
    print(f"Mutations: {len(MUTATIONS)} tests in {len({m.group for m in MUTATIONS})} groups")

    if args.preview_only:
        img.save("fuzz_test_image.png")
        print("Saved: fuzz_test_image.png")
        return

    if args.dry_run:
        print()
        for m in MUTATIONS:
            mutated = apply_mutation(baseline, m)
            print_mutation_info(m, baseline, mutated)
        print(f"\n{len(MUTATIONS)} mutations total. Use without --dry-run to send.")
        return

    # Connect
    print(f"\nScanning for {args.device}...")
    target = await find_device(
        device_name=args.device,
        device_address=getattr(args, "address", None),
        timeout=SCAN_TIMEOUT,
        scan_retries=3,
        prefixes=(profile.device_prefix,),
    )
    if not target:
        print("Device not found")
        return

    session = await connect_session(
        target.get("_ble_device") or target["address"],
        timeout=20.0,
        connect_retries=CONNECT_RETRIES,
    )
    if not session:
        print("Connection failed")
        return

    print(f"Connected: {target['name']} ({target['address']})")

    async def reconnect():
        nonlocal session
        print("  reconnecting...")
        await session.close()
        s = await connect_session(
            target.get("_ble_device") or target["address"],
            timeout=20.0,
            connect_retries=CONNECT_RETRIES,
        )
        if not s:
            print("  reconnect failed!")
            return False
        session = s
        print("  reconnected")
        return True

    try:
        # Baseline send
        print(f"\n--- BASELINE (unmodified frame) ---")
        ok = await send_frame(session, baseline, args.interval)
        if not ok:
            if not await reconnect():
                return
            ok = await send_frame(session, baseline, args.interval)
            if not ok:
                print("Baseline send failed after reconnect, aborting")
                return

        await async_input("  Baseline sent. Observe display, press Enter to start fuzzing...")

        # Determine start point
        start_idx = 0
        if args.start_from:
            for i, m in enumerate(MUTATIONS):
                if m.id.upper() == args.start_from.upper():
                    start_idx = i
                    break
            else:
                print(f"Unknown mutation ID: {args.start_from}")
                return
            if start_idx > 0:
                print(f"Skipping to {MUTATIONS[start_idx].id}")

        current_group = None
        skip_group = None

        for m in MUTATIONS[start_idx:]:
            if skip_group and m.group == skip_group:
                continue
            skip_group = None

            if m.group != current_group:
                current_group = m.group
                print(f"\n{'─'*60}")
                print(f"  GROUP {current_group}")
                print(f"{'─'*60}")

            mutated = apply_mutation(baseline, m)
            print_mutation_info(m, baseline, mutated)

            ok = await send_frame(session, bytes(mutated), args.interval)
            if not ok:
                if not await reconnect():
                    return
                ok = await send_frame(session, bytes(mutated), args.interval)
                if not ok:
                    print("  Send failed after reconnect")

            cmd = ""
            while True:
                cmd = (await async_input(
                    "  [Enter]=next  [r]=resend  [b]=baseline  [s]=skip group  [q]=quit: "
                )).strip().lower()

                if cmd in ("", "n", "s", "q"):
                    break
                elif cmd == "r":
                    ok = await send_frame(session, bytes(mutated), args.interval)
                    if not ok and not await reconnect():
                        return
                elif cmd == "b":
                    print("  Sending baseline...")
                    ok = await send_frame(session, baseline, args.interval)
                    if not ok and not await reconnect():
                        return

                await asyncio.sleep(0.5)

            if cmd == "q":
                print("Quitting.")
                return
            if cmd == "s":
                skip_group = m.group

            await asyncio.sleep(args.delay)

        print("\nAll mutations sent!")

    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        await session.close()
        print("Disconnected.")


def main():
    parser = argparse.ArgumentParser(
        description="Fuzz EPD header bytes to find faster refresh modes",
    )
    parser.add_argument("--device", "-d", help="Device name e.g. EPD-E1FBDFD8")
    parser.add_argument("--address", "-a", help="BLE address")
    parser.add_argument("--interval", type=int, default=DEFAULT_INTERVAL_MS,
                        help="Packet interval ms (default 50)")
    parser.add_argument("--delay", type=float, default=DEFAULT_DELAY,
                        help="Seconds between tests (default 8)")
    parser.add_argument("--start-from", help="Start from mutation ID (e.g. B3)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print mutations without BLE")
    parser.add_argument("--preview-only", action="store_true",
                        help="Save test image and exit")
    args = parser.parse_args()

    if not args.preview_only and not args.dry_run and not args.device:
        parser.error("--device is required (unless --preview-only or --dry-run)")

    asyncio.run(run(args))


if __name__ == "__main__":
    main()
