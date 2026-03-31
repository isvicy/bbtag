"""
BLE 通信模块 — 扫描、连接、发送

依赖 bleak 库。
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable


class BleDependencyError(ModuleNotFoundError):
    """Raised when the optional BLE dependency is not installed."""


def _require_bleak():
    try:
        from bleak import BleakClient, BleakScanner
    except ModuleNotFoundError as exc:
        if exc.name != "bleak":
            raise
        raise BleDependencyError(
            "BLE support requires `bleak`. Run `uv sync` to install project dependencies."
        ) from exc
    return BleakClient, BleakScanner


SERVICE_UUID = "0000ffe0-0000-1000-8000-00805f9b34fb"
WRITE_UUID = "0000ffe2-0000-1000-8000-00805f9b34fb"
NOTIFY_UUID = "0000ffe1-0000-1000-8000-00805f9b34fb"

DEFAULT_DEVICE_PREFIX = "EPD-"
SCAN_TIMEOUT = 15.0
CONNECT_TIMEOUT = 15.0
PACKET_INTERVAL = 0.05  # 50ms/包


def _normalize_prefixes(prefixes: Iterable[str] | None) -> tuple[str, ...]:
    if prefixes is None:
        return (DEFAULT_DEVICE_PREFIX,)

    normalized = tuple(prefix for prefix in prefixes if prefix)
    return normalized or (DEFAULT_DEVICE_PREFIX,)


def _matches_prefix(name: str | None, prefixes: tuple[str, ...]) -> bool:
    return bool(name) and any(name.startswith(prefix) for prefix in prefixes)


def _resolve_read_uuid(client) -> str | None:
    if not client.services:
        return None

    for service in client.services:
        if service.uuid.lower() != SERVICE_UUID:
            continue

        preferred = None
        readable: list[str] = []
        for char in service.characteristics:
            properties = {prop.lower() for prop in char.properties}
            if "read" not in properties:
                continue
            readable.append(char.uuid)
            if char.uuid.lower() == NOTIFY_UUID:
                preferred = char.uuid
        return preferred or (readable[0] if readable else None)

    return None


class BleSession:
    """Connected BLE session with optional flush support."""

    def __init__(self, device_ref, timeout: float = CONNECT_TIMEOUT):
        self.device_ref = device_ref
        self.timeout = timeout
        self.client = None
        self.read_uuid: str | None = None

    async def open(self) -> "BleSession":
        BleakClient, _ = _require_bleak()
        self.client = BleakClient(self.device_ref, timeout=self.timeout)
        await self.client.connect()
        await asyncio.sleep(1.0)

        services = self.client.services
        if not services:
            raise RuntimeError("未发现 GATT services")

        if not any(service.uuid.lower() == SERVICE_UUID for service in services):
            raise RuntimeError(f"未找到所需服务 {SERVICE_UUID}")

        self.read_uuid = _resolve_read_uuid(self.client)

        try:
            await self.client.start_notify(NOTIFY_UUID, lambda _sender, _data: None)
        except Exception:
            # Some devices do not require notify to be enabled before writes.
            pass
        return self

    async def close(self):
        if self.client and self.client.is_connected:
            await self.client.disconnect()

    async def write(self, data: bytes, response: bool = False):
        if not self.client or not self.client.is_connected:
            raise RuntimeError("BLE session is not connected")
        await self.client.write_gatt_char(WRITE_UUID, data, response=response)

    async def flush(self) -> bool:
        if not self.client or not self.client.is_connected or not self.read_uuid:
            return False
        await self.client.read_gatt_char(self.read_uuid)
        return True

    async def __aenter__(self) -> "BleSession":
        return await self.open()

    async def __aexit__(self, exc_type, exc, tb):
        await self.close()


async def scan(
    timeout: float = SCAN_TIMEOUT,
    prefixes: Iterable[str] | None = None,
) -> list[dict]:
    """
    扫描附近的蓝签设备。

    Returns:
        list[dict]: [{"name": "EPD-...", "address": "...", "rssi": -50}, ...]
    """
    _, BleakScanner = _require_bleak()
    resolved_prefixes = _normalize_prefixes(prefixes)
    results = await BleakScanner.discover(timeout=timeout, return_adv=True)
    devices = []
    for _addr, (device, adv) in results.items():
        if _matches_prefix(device.name, resolved_prefixes):
            devices.append(
                {
                    "name": device.name,
                    "address": device.address,
                    "rssi": adv.rssi,
                    "_ble_device": device,
                }
            )
    return devices


async def find_device(
    device_name: str | None = None,
    device_address: str | None = None,
    *,
    timeout: float = SCAN_TIMEOUT,
    scan_retries: int = 3,
    prefixes: Iterable[str] | None = None,
) -> dict | None:
    """扫描并查找目标设备。"""
    for _attempt in range(scan_retries):
        devices = await scan(timeout=timeout, prefixes=prefixes)
        for device in devices:
            if device_address and device["address"].lower() == device_address.lower():
                return device
            if device_name and device["name"] == device_name:
                return device
            if not device_name and not device_address:
                return device
    return None


async def connect_session(
    device_ref,
    *,
    timeout: float = CONNECT_TIMEOUT,
    connect_retries: int = 3,
) -> BleSession | None:
    """连接到设备并返回可复用的 BLE 会话。"""
    for attempt in range(connect_retries):
        session = BleSession(device_ref, timeout=timeout)
        try:
            return await session.open()
        except Exception as exc:
            await session.close()
            print(f"  连接失败 ({attempt + 1}/{connect_retries}): {exc}")
            if attempt < connect_retries - 1:
                await asyncio.sleep(2)
    return None


async def push(
    packets: list[bytes],
    device_name: str = None,
    device_address: str = None,
    scan_retries: int = 3,
    connect_retries: int = 3,
    packet_interval: float = PACKET_INTERVAL,
    on_progress: callable = None,
    prefixes: Iterable[str] | None = None,
    scan_timeout: float = SCAN_TIMEOUT,
) -> bool:
    """
    通过 BLE 发送数据包到蓝签设备。

    Args:
        packets: packetize() 的输出
        device_name: 目标设备名 (如 "EPD-EBB9D76B")，None 则连接第一个发现的
        device_address: 目标设备 BLE 地址，优先于 device_name
        scan_retries: 扫描重试次数
        connect_retries: 连接重试次数
        packet_interval: 包间隔 (秒)，默认 0.05
        on_progress: 回调 fn(sent, total)，可选
        prefixes: 设备名前缀过滤
        scan_timeout: 单次扫描超时

    Returns:
        bool: 是否发送成功
    """
    target = await find_device(
        device_name=device_name,
        device_address=device_address,
        timeout=scan_timeout,
        scan_retries=scan_retries,
        prefixes=prefixes,
    )
    if not target:
        return False

    session = await connect_session(
        target.get("_ble_device") or target["address"],
        timeout=CONNECT_TIMEOUT,
        connect_retries=connect_retries,
    )
    if not session:
        return False

    try:
        total = len(packets)
        for index, packet in enumerate(packets, start=1):
            await session.write(packet, response=False)
            await asyncio.sleep(packet_interval)
            if on_progress:
                on_progress(index, total)
        return True
    except Exception:
        return False
    finally:
        await session.close()
