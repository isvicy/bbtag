"""Transmission helpers for layer-based e-ink screens."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from bluetag.ble import BleSession

BLACK_TYPE = 0x13
RED_TYPE = 0x12
LAYER_PAYLOAD_SIZE = 16
START_PACKET = bytes([0x00, 0x00, 0x00, 0x00])
END_PACKET = bytes([0xFF, 0xFF, 0xFF, 0xFF])

ProgressCallback = Callable[[str, int, int], None]


async def _send_layer(
    session: BleSession,
    data: bytes,
    *,
    layer_type: int,
    layer_name: str,
    delay_ms: int,
    flush_every: int,
    on_progress: ProgressCallback | None,
) -> bool:
    try:
        await session.write(bytes([layer_type]) + START_PACKET, response=False)
        await asyncio.sleep(delay_ms / 1000.0)

        total_packets = (len(data) + LAYER_PAYLOAD_SIZE - 1) // LAYER_PAYLOAD_SIZE
        await asyncio.sleep(1.0)

        first_packet_sent = False
        writes_since_flush = 1

        packet_index = 1
        offset = 0
        while offset < len(data):
            chunk_size = min(LAYER_PAYLOAD_SIZE, len(data) - offset)
            chunk = data[offset : offset + chunk_size]
            packet = bytes([layer_type, packet_index & 0xFF, chunk_size]) + chunk

            await session.write(packet, response=False)
            if not first_packet_sent:
                await asyncio.sleep(delay_ms / 1000.0)
                await session.write(packet, response=False)
                first_packet_sent = True

            await asyncio.sleep(delay_ms / 1000.0)
            writes_since_flush += 1
            if flush_every > 0 and writes_since_flush >= flush_every:
                await session.flush()
                writes_since_flush = 0

            offset += chunk_size
            if on_progress:
                on_progress(layer_name, packet_index, total_packets)
            packet_index += 1

        await session.write(bytes([layer_type]) + END_PACKET, response=False)
        await asyncio.sleep(delay_ms / 1000.0)

        if flush_every > 0:
            await session.flush()
        return True
    except Exception as exc:
        print(f"\n❌ {layer_name}发送失败: {exc}")
        return False


async def send_bicolor_image(
    session: BleSession,
    black_data: bytes,
    red_data: bytes,
    *,
    delay_ms: int,
    settle_ms: int,
    flush_every: int = 0,
    on_progress: ProgressCallback | None = None,
) -> bool:
    """Send black and red layers using the small-screen legacy format."""
    if not await _send_layer(
        session,
        black_data,
        layer_type=BLACK_TYPE,
        layer_name="黑层",
        delay_ms=delay_ms,
        flush_every=flush_every,
        on_progress=on_progress,
    ):
        return False

    await asyncio.sleep(0.1)

    if not await _send_layer(
        session,
        red_data,
        layer_type=RED_TYPE,
        layer_name="红层",
        delay_ms=delay_ms,
        flush_every=flush_every,
        on_progress=on_progress,
    ):
        return False

    if settle_ms > 0:
        await asyncio.sleep(settle_ms / 1000.0)

    return True
