"""
示例: 推送文字到蓝签设备

用法:
    uv run examples/push_text.py "要显示的文字"
    uv run examples/push_text.py "正文内容" --title "标题"
    uv run examples/push_text.py "会议室A\n14:00-15:30\n项目评审" --title "今日日程" --preview-only
"""

import asyncio
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from bluetag import quantize, pack_2bpp, build_frame, packetize, render_text
from bluetag.image import indices_to_image
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
    import argparse

    parser = argparse.ArgumentParser(description="推送文字到蓝签设备")
    parser.add_argument("body", help="正文内容 (用 \\n 表示换行)")
    default_title = f"{date.today():%Y-%m-%d}"
    parser.add_argument(
        "--title", "-T", default=default_title, help=f"标题 (默认 {default_title})"
    )
    parser.add_argument(
        "--title-color", default="red", choices=["black", "red", "yellow"]
    )
    parser.add_argument(
        "--body-color", default="black", choices=["black", "red", "yellow"]
    )
    parser.add_argument("--align", default="left", choices=["left", "center"])
    parser.add_argument("--preview-only", action="store_true", help="仅生成预览图")
    args = parser.parse_args()

    body = args.body.replace("\\n", "\n")

    # 1. 渲染文字图像
    img = render_text(
        body=body,
        title=args.title,
        title_color=args.title_color,
        body_color=args.body_color,
        align=args.align,
    )

    # 2. 量化 + 预览
    indices = quantize(img)
    data_2bpp = pack_2bpp(indices)

    preview = indices_to_image(indices)
    preview.save("text_preview.png")
    print("预览已保存: text_preview.png")

    if args.preview_only:
        return 0

    try:
        from bluetag.ble import scan, push

        # 3. 扫描设备 (优先读缓存)
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

        # 4. 组帧 + 发送
        mac_suffix = parse_mac_suffix(target["name"])
        frame = build_frame(mac_suffix, data_2bpp)
        packets = packetize(frame)
        print(f"帧: {len(frame)} bytes, {len(packets)} 包")

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
