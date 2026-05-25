import os
import subprocess
import tempfile
import httpx
from fastapi import APIRouter, HTTPException, UploadFile, File
from pydantic import BaseModel
from typing import Optional

router = APIRouter(prefix="/wa", tags=["whatsapp"])

WA_TOKEN = os.getenv("WA_TOKEN")
WA_PHONE_ID = os.getenv("WA_PHONE_ID")


def _base() -> str:
    return f"https://graph.facebook.com/v19.0/{WA_PHONE_ID}"


def _auth() -> dict:
    return {"Authorization": f"Bearer {WA_TOKEN}"}


class TextPayload(BaseModel):
    to: str
    text: str


class MediaPayload(BaseModel):
    to: str
    type: str          # image | video | audio
    media_id: str
    caption: Optional[str] = None


@router.post("/send")
async def send_text(payload: TextPayload):
    """Envía mensaje de texto por WhatsApp."""
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            f"{_base()}/messages",
            headers={**_auth(), "Content-Type": "application/json"},
            json={
                "messaging_product": "whatsapp",
                "to": payload.to,
                "type": "text",
                "text": {"body": payload.text},
            },
        )
    if not r.is_success:
        raise HTTPException(status_code=r.status_code, detail=r.json())
    return r.json()


def _convert_audio_to_opus(content: bytes) -> tuple[bytes, str]:
    """Convert audio bytes to ogg/opus using ffmpeg. Returns (converted_bytes, filename)."""
    with tempfile.NamedTemporaryFile(suffix=".webm", delete=False) as src:
        src.write(content)
        src_path = src.name
    dst_path = src_path.replace(".webm", ".ogg")
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-i", src_path, "-c:a", "libopus", "-b:a", "48k", dst_path],
            check=True,
            capture_output=True,
        )
        with open(dst_path, "rb") as f:
            return f.read(), "audio.ogg"
    finally:
        os.unlink(src_path)
        if os.path.exists(dst_path):
            os.unlink(dst_path)


@router.post("/upload")
async def upload_media(file: UploadFile = File(...)):
    """Sube un archivo a WhatsApp Media y devuelve su media_id."""
    content = await file.read()
    base_type = (file.content_type or "application/octet-stream").split(";")[0].strip()

    upload_content = content
    upload_filename = file.filename or "upload"
    upload_type = base_type

    if base_type.startswith("audio/"):
        upload_content, upload_filename = _convert_audio_to_opus(content)
        upload_type = "audio/ogg"

    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(
            f"{_base()}/media",
            headers=_auth(),
            data={"messaging_product": "whatsapp", "type": upload_type},
            files={"file": (upload_filename, upload_content, upload_type)},
        )
    if not r.is_success:
        raise HTTPException(status_code=r.status_code, detail=r.json())
    return r.json()   # {id: "..."}


@router.post("/media")
async def send_media(payload: MediaPayload):
    """Envía un mensaje de imagen, video o audio usando un media_id ya subido."""
    media_obj: dict = {"id": payload.media_id}
    if payload.type == "audio":
        media_obj["voice"] = True
    if payload.caption:
        media_obj["caption"] = payload.caption
    body = {
        "messaging_product": "whatsapp",
        "to": payload.to,
        "type": payload.type,
        payload.type: media_obj,
    }
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            f"{_base()}/messages",
            headers={**_auth(), "Content-Type": "application/json"},
            json=body,
        )
    if not r.is_success:
        raise HTTPException(status_code=r.status_code, detail=r.json())
    return r.json()
