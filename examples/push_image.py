"""
示例: 推送图片到蓝签设备

用法:
    uv run push_image.py <image_path>
"""

import asyncio
import sys
from pathlib import Path
from PIL import Image

# 如果未安装 bluetag 包，添加父目录到 path
sys.path.insert(0, str(Path(__file__).parent.parent))

from bluetag import quantize, pack_2bpp, build_frame, packetize
from bluetag.ble import BleDependencyError
from bluetag.protocol import parse_mac_suffix

DEVICE_FILE = Path(__file__).parent.parent / ".device"


def _save_device(device: dict):
    DEVICE_FILE.write_text(f"{device['name']}\n{device['address']}\n")


def _load_device() -> dict | None:
    if not DEVICE_FILE.exists():
        return None
    lines = DEVICE_FILE.read_text().strip().splitlines()
    if len(lines) >= 2:
        return {"name": lines[0], "address": lines[1]}
    return None


async def main():
    if len(sys.argv) < 2:
        print("用法: uv run push_image.py <image_path>")
        return 1

    image_path = sys.argv[1]
    img = Image.open(image_path)

    # 1. 图像处理 (纯 CPU，无 BLE 依赖)
    indices = quantize(img)
    data_2bpp = pack_2bpp(indices)

    try:
        from bluetag.ble import scan, push

        # 2. 扫描设备 (优先读缓存)
        target = _load_device()
        if target:
            print(f"使用缓存设备: {target['name']} ({target['address']})")
        else:
            print("扫描设备...")
            devices = await scan()
            if not devices:
                print("未找到蓝签设备")
                return 1
            target = devices[0]
            _save_device(target)

        print(f"找到: {target['name']}")

        # 3. 组装协议帧 (纯 CPU)
        mac_suffix = parse_mac_suffix(target["name"])
        frame = build_frame(mac_suffix, data_2bpp)
        packets = packetize(frame)
        print(f"帧: {len(frame)} bytes, {len(packets)} 包")

        # 4. BLE 发送
        ok = await push(
            packets,
            device_address=target["address"],
            on_progress=lambda s, t: (
                print(f"\r  {s}/{t}", end="", flush=True) if s % 10 == 0 else None
            ),
        )
        print(f"\n{'✅ 成功' if ok else '❌ 失败'}")
        return 0 if ok else 1
    except BleDependencyError as exc:
        print(f"❌ {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
