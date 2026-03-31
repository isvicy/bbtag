"""
BluETag — BT370R 蓝签电子墨水标签 BLE 图像推送库

支持 240×416 4色 (黑白黄红) 电子墨水标签的图像编码、协议组装和 BLE 传输。
"""

__version__ = "1.0.0"

__all__ = [
    "quantize",
    "pack_2bpp",
    "unpack_2bpp",
    "indices_to_image",
    "build_frame",
    "packetize",
    "render_text",
]


def __getattr__(name: str):
    if name in {"quantize", "pack_2bpp", "unpack_2bpp", "indices_to_image"}:
        from bluetag.image import (
            indices_to_image,
            pack_2bpp,
            quantize,
            unpack_2bpp,
        )

        return {
            "quantize": quantize,
            "pack_2bpp": pack_2bpp,
            "unpack_2bpp": unpack_2bpp,
            "indices_to_image": indices_to_image,
        }[name]

    if name in {"build_frame", "packetize"}:
        from bluetag.protocol import build_frame, packetize

        return {"build_frame": build_frame, "packetize": packetize}[name]

    if name == "render_text":
        from bluetag.text import render_text

        return render_text

    raise AttributeError(f"module 'bluetag' has no attribute {name!r}")
