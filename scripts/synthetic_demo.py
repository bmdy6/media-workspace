"""Local-only workflow fixture. No provider, COS or microphone calls.

Run from the source checkout with: python scripts/synthetic_demo.py
Use demo / synthetic-demo-password. Temporary data is removed on exit.
This is an integration fixture, not evidence of real image generation.
"""
from __future__ import annotations

import io
import json
import os
from pathlib import Path
import secrets
import sys
import tempfile
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
_temporary = tempfile.TemporaryDirectory(prefix="media-workspace-demo-")
os.environ["MEDIA_DATA_DIR"] = _temporary.name
os.environ["MEDIA_USE_MYSQL"] = "0"
os.environ["SECRET_KEY"] = secrets.token_urlsafe(48)
os.environ["MEDIA_CORS_ORIGINS"] = "http://127.0.0.1:8765"

from PIL import Image, ImageDraw
from fastapi.responses import Response
import httpx
import requests
import uvicorn

from media.agent.chat_agent import chat_agent
from media.core.cos_storage import cos_storage
from media.core.image_service import image_core, ProviderImageResult
from media.database import init_db, session_scope, User
from media.web.backend.auth import hash_password
from media.web.backend.main import app
from media.web.backend.api import voice_router

_objects: dict[str, bytes] = {}
_calls = {"chat": 0, "image": 0, "with_reference": 0, "storage": 0}
_object_prefix = "http://127.0.0.1:8765/demo/objects/"


def fixture_png(color: str) -> bytes:
    image = Image.new("RGB", (1024, 1024), "#f6f0e7")
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((180, 230, 840, 800), radius=80, fill=color)
    draw.ellipse((350, 320, 675, 645), fill="#fff7e9")
    draw.text((370, 690), "SYNTHETIC DEMO", fill="white")
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def upload(data, filename, **kwargs):
    _calls["storage"] += 1
    key = uuid.uuid4().hex + ".png"
    _objects[key] = data
    return _object_prefix + key


def stream_reply(messages, *, model_id, **kwargs):
    _calls["chat"] += 1
    serialized = json.dumps(messages, ensure_ascii=False)
    modification = "橙色" in serialized
    card = {"title": "橙色构图修改方案" if modification else "蓝色几何构图",
            "design_rationale": "保留中心构图，并在确认后生成。此回复来自本地合成测试。",
            "prompt": "橙色几何构图，浅色背景" if modification else "蓝色几何构图，浅色背景",
            "style": "default", "aspect_ratio": "1:1", "is_modification": modification,
            "reference_recommended": not modification, "reference_reason": "可以绑定参考图辅助构图"}
    reply = "这是本地合成方案。请检查提示词，再明确确认生成。\n```json\n" + json.dumps({"card": card}, ensure_ascii=False) + "\n```"
    for index in range(0, len(reply), 24):
        time.sleep(.02)
        yield {"kind": "content", "text": reply[index:index + 24]}
    yield {"kind": "complete", "model_id": model_id}


def generate(**kwargs):
    _calls["image"] += 1
    if kwargs.get("ref_bytes") or kwargs.get("ref_image_bytes"):
        _calls["with_reference"] += 1
    time.sleep(.8)
    return ProviderImageResult(fixture_png("#d47835" if _calls["image"] > 1 else "#4263b8"), None, kwargs.get("model_id") or "gpt-image-2")


def no_network(*args, **kwargs):
    raise RuntimeError("The synthetic demo does not allow external network requests")


init_db()
with session_scope() as db:
    db.add(User(username="demo", password_hash=hash_password("synthetic-demo-password"), is_admin=True))
cos_storage.upload_bytes = upload
cos_storage.public_object_key = lambda url: url[len(_object_prefix):] if url.startswith(_object_prefix) else None
cos_storage.download_bytes = lambda key, **kwargs: _objects[key]
chat_agent._iter_deepseek_stream = stream_reply
image_core._call_transit_api = generate
voice_router.stream_text = lambda *args, **kwargs: iter(["合成测试的", "整理结果"])
voice_router.complete_text = lambda *args, **kwargs: "合成测试的整理结果"
requests.Session.request = no_network
httpx.Client.send = no_network
httpx.AsyncClient.send = no_network


# Insert fixture routes before the production SPA catch-all, without changing it.
from fastapi import APIRouter
fixture_router = APIRouter()


@fixture_router.get("/demo/objects/{key}")
def object_image(key: str):
    return Response(_objects.get(key, b""), media_type="image/png", status_code=200 if key in _objects else 404)


@fixture_router.get("/demo/metrics")
def metrics():
    return _calls


app.router.routes[0:0] = fixture_router.routes

if __name__ == "__main__":
    print("Synthetic demo: http://127.0.0.1:8765 (demo / synthetic-demo-password)", flush=True)
    try:
        uvicorn.run(app, host="127.0.0.1", port=8765, log_level="warning")
    finally:
        _temporary.cleanup()
