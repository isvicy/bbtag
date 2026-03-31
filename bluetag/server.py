"""
BluETag Server — BLE 图像推送 API 服务

运行在 BLE 主机上 (Mac Mini / 树莓派)，暴露 REST API 供远程调用。
"""

import asyncio
import hmac
import io
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from PIL import Image
from pydantic_settings import BaseSettings

from bluetag import __version__, build_frame, pack_2bpp, packetize, quantize
from bluetag.ble import scan as ble_scan
from bluetag.ble import push as ble_push
from bluetag.protocol import parse_mac_suffix


class Settings(BaseSettings):
    api_token: str = ""
    cors_origins: str = "*"
    scan_interval: int = 60  # 自动扫描间隔 (秒)
    packet_interval: int = 50  # BLE 包间隔 (ms)
    host: str = "0.0.0.0"
    port: int = 8090

    class Config:
        env_prefix = "BLUETAG_"
        env_file = ".env"


settings = Settings()

# 设备缓存
device_cache: dict[str, dict] = {}  # {name: {name, address, rssi, last_seen}}
cache_lock = asyncio.Lock()


async def periodic_scan():
    """后台定期扫描 BLE 设备"""
    while True:
        try:
            devices = await ble_scan(timeout=10.0)
            async with cache_lock:
                now = time.time()
                for d in devices:
                    device_cache[d["name"]] = {**d, "last_seen": now}
        except Exception:
            pass
        await asyncio.sleep(settings.scan_interval)


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(periodic_scan())
    yield
    task.cancel()


app = FastAPI(
    title="BluETag Server",
    version=__version__,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins.split(","),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─── Auth ────────────────────────────────────────────────


def verify_token(request: Request):
    if not settings.api_token:
        raise HTTPException(503, "API token not configured")
    token = request.headers.get("X-API-Token") or request.query_params.get("token")
    if not token or not hmac.compare_digest(token, settings.api_token):
        raise HTTPException(401, "Invalid API token")


# ─── Routes ──────────────────────────────────────────────


@app.get("/api/health")
async def health():
    return {
        "status": "ok",
        "version": __version__,
        "devices": len(device_cache),
    }


@app.get("/api/v1/devices")
async def list_devices(request: Request):
    verify_token(request)
    async with cache_lock:
        now = time.time()
        devices = []
        for d in device_cache.values():
            devices.append(
                {
                    **d,
                    "online": (now - d["last_seen"]) < settings.scan_interval * 2,
                }
            )
    return {"items": devices, "total": len(devices)}


@app.post("/api/v1/devices/scan")
async def trigger_scan(request: Request):
    verify_token(request)
    devices = await ble_scan(timeout=10.0)
    async with cache_lock:
        now = time.time()
        for d in devices:
            device_cache[d["name"]] = {**d, "last_seen": now}
    return {"items": devices, "total": len(devices)}


@app.post("/api/v1/push")
async def push_image(
    request: Request,
    file: UploadFile = File(...),
    device: str = Query(
        None, description="设备名 (如 EPD-EBB9D76B)，不指定则推送到第一个在线设备"
    ),
):
    verify_token(request)

    # 读取图片
    try:
        data = await file.read()
        img = Image.open(io.BytesIO(data))
    except Exception:
        raise HTTPException(400, "Invalid image file")

    # 查找目标设备
    async with cache_lock:
        if device:
            target = device_cache.get(device)
            if not target:
                raise HTTPException(404, f"Device '{device}' not found")
        else:
            online = [
                d
                for d in device_cache.values()
                if (time.time() - d["last_seen"]) < settings.scan_interval * 2
            ]
            if not online:
                raise HTTPException(404, "No online devices")
            target = online[0]

    # 编码
    indices = quantize(img)
    data_2bpp = pack_2bpp(indices)
    mac_suffix = parse_mac_suffix(target["name"])
    frame = build_frame(mac_suffix, data_2bpp)
    packets = packetize(frame)

    # 发送
    ok = await ble_push(
        packets,
        device_address=target["address"],
        packet_interval=settings.packet_interval / 1000,
    )

    if not ok:
        raise HTTPException(502, "BLE push failed")

    return {
        "status": "ok",
        "device": target["name"],
        "packets": len(packets),
        "frame_size": len(frame),
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=settings.host, port=settings.port)
