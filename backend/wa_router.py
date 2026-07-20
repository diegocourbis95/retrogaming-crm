import os
import subprocess
import tempfile
import httpx
import logging
from datetime import datetime
from fastapi import APIRouter, HTTPException, UploadFile, File, Query, Request
from pydantic import BaseModel
from typing import Optional

from limiter import limiter

_log = logging.getLogger(__name__)

router = APIRouter(prefix="/wa", tags=["whatsapp"])

WA_TOKEN = os.getenv("WA_TOKEN")
WA_PHONE_ID = os.getenv("WA_PHONE_ID")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
STORAGE_BASE_URL = os.getenv("STORAGE_BASE_URL")

# Límites de tamaño de Meta Cloud API (WhatsApp), usados como techo propio
# para no gastar RAM/tiempo subiendo archivos que Meta rechazaría de todas formas.
MAX_SIZE_IMAGE = 5 * 1024 * 1024      # 5MB
MAX_SIZE_VIDEO = 16 * 1024 * 1024     # 16MB
MAX_SIZE_AUDIO = 16 * 1024 * 1024     # 16MB
MAX_SIZE_DOCUMENT = 5 * 1024 * 1024   # 5MB (Meta permite hasta 100MB, pero para boletas/PDF alcanza de sobra)

# Firmas (magic bytes) de los formatos que este endpoint procesa. Evita confiar
# solo en el content-type declarado por el cliente, que se puede falsificar.
_MAGIC_SIGNATURES = {
    "image": [b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n", b"GIF87a", b"GIF89a"],
    "video": [b"ftyp"],  # aparece en el offset 4 de contenedores MP4/3GP/MOV
    "audio": [b"ID3", b"\xff\xfb", b"\xff\xf3", b"\xff\xf2", b"\xff\xf1", b"\xff\xf9", b"OggS", b"#!AMR"],
    "document": [b"%PDF-"],
}


def _detect_real_category(content: bytes) -> Optional[str]:
    """Detecta la categoría real del archivo por sus magic bytes (ignora el content-type declarado)."""
    head = content[:16]
    if head[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return "image"
    if b"ftyp" in head:
        return "video"
    for category, signatures in _MAGIC_SIGNATURES.items():
        if any(head.startswith(sig) for sig in signatures):
            return category
    return None


def _base() -> str:
    return f"https://graph.facebook.com/v19.0/{WA_PHONE_ID}"


def _auth() -> dict:
    return {"Authorization": f"Bearer {WA_TOKEN}"}


class TextPayload(BaseModel):
    to: str
    text: str


class MediaPayload(BaseModel):
    to: str
    type: str          # image | video | audio | document
    media_id: str
    caption: Optional[str] = None
    filename: Optional[str] = None


TEMPLATES = {
    "seguimiento_sin_respuesta",
    "reactivacion_conversacion",
    "coordinacion_despacho",
}


class TemplatePayload(BaseModel):
    to: str
    template_name: str
    producto: str


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
    response_data = r.json()
    message_id = response_data.get("messages", [{}])[0].get("id")
    return {"ok": True, "message_id": message_id}


@router.post("/template")
async def send_template(payload: TemplatePayload):
    """Envía un template aprobado de WhatsApp con el producto como parámetro {{1}}."""
    if payload.template_name not in TEMPLATES:
        raise HTTPException(status_code=400, detail=f"Template desconocido: {payload.template_name}")

    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            f"{_base()}/messages",
            headers={**_auth(), "Content-Type": "application/json"},
            json={
                "messaging_product": "whatsapp",
                "to": payload.to,
                "type": "template",
                "template": {
                    "name": payload.template_name,
                    "language": {"code": "es"},
                    "components": [
                        {
                            "type": "body",
                            "parameters": [{"type": "text", "text": payload.producto}],
                        }
                    ],
                },
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


# ⚠️ CRÍTICO: Esta función es OBLIGATORIA para el envío de videos desde el CRM.
# WhatsApp Cloud API solo acepta H.264/AAC. Los celulares modernos graban en H.265
# que Meta rechaza silenciosamente (retorna 200 OK pero el video no llega).
# NUNCA eliminar ni saltarse esta conversión. Si se elimina, los videos dejarán
# de llegar a los clientes sin mostrar ningún error visible.
# Historial: bug detectado el 2026-06-08, resuelto con conversión ffmpeg automática.
def _convert_video_to_h264(content: bytes, original_filename: str) -> tuple[bytes, str]:
    """
    Convierte un video a H.264/AAC usando ffmpeg para compatibilidad con WhatsApp.

    Args:
        content: Bytes del video original
        original_filename: Nombre del archivo original (para mantener extensión)

    Returns:
        (converted_bytes, filename): Video convertido y nombre del archivo

    Raises:
        Exception: Si la conversión falla
    """
    # Crear archivo temporal de entrada
    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as src:
        src.write(content)
        src_path = src.name

    # Crear path de salida
    dst_path = src_path.replace(".mp4", "_converted.mp4")

    try:
        _log.info("🎬 Convirtiendo video a H.264: %d bytes", len(content))

        # Comando ffmpeg optimizado para WhatsApp:
        # -vcodec libx264: Video codec H.264
        # -acodec aac: Audio codec AAC
        # -preset fast: Balance entre velocidad y compresión
        # -crf 23: Calidad (18-28, menor = mejor calidad)
        # -movflags +faststart: Permite streaming (importante para WhatsApp)
        # -y: Sobrescribir archivo de salida
        subprocess.run(
            [
                "ffmpeg",
                "-y",                          # Sobrescribir sin preguntar
                "-i", src_path,                # Input
                "-vcodec", "libx264",          # Video: H.264
                "-acodec", "aac",              # Audio: AAC
                "-preset", "fast",             # Velocidad de encoding
                "-crf", "23",                  # Calidad (23 = buena calidad)
                "-movflags", "+faststart",     # Optimizar para streaming
                "-max_muxing_queue_size", "1024",  # Evitar errores de buffer
                dst_path
            ],
            check=True,
            capture_output=True,
            timeout=120  # Timeout de 2 minutos
        )

        # Leer archivo convertido
        with open(dst_path, "rb") as f:
            converted_content = f.read()

        converted_filename = original_filename or "video_converted.mp4"

        _log.info("✅ Video convertido: %d bytes → %d bytes (%.1f%% del original)",
                 len(content), len(converted_content),
                 (len(converted_content) / len(content)) * 100)

        return converted_content, converted_filename

    except subprocess.TimeoutExpired:
        _log.error("❌ Timeout convirtiendo video (>120s)")
        raise Exception("Video conversion timeout - archivo demasiado grande o corrupto")
    except subprocess.CalledProcessError as e:
        stderr = e.stderr.decode('utf-8', errors='ignore') if e.stderr else ""
        _log.error("❌ Error ffmpeg: %s", stderr[-500:])  # Últimos 500 chars del error
        raise Exception(f"Video conversion failed: {stderr[-200:]}")
    finally:
        # Limpiar archivos temporales
        try:
            os.unlink(src_path)
        except:
            pass
        try:
            if os.path.exists(dst_path):
                os.unlink(dst_path)
        except:
            pass


async def _upload_to_supabase_storage(content: bytes, phone: str, filename: str, content_type: str) -> str:
    """
    Sube un archivo (video/imagen/audio) a Supabase Storage y retorna la URL pública.

    Args:
        content: Bytes del archivo
        phone: Teléfono del cliente (para organizar carpetas)
        filename: Nombre del archivo
        content_type: MIME type del archivo

    Returns:
        URL pública del archivo en Supabase Storage

    Raises:
        Exception: Si el upload falla
    """
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise Exception("SUPABASE_URL o SUPABASE_KEY no configurados en .env")

    # Generar path único: chats/{phone}/timestamp_filename
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    storage_path = f"chats/{phone}/{timestamp}_{filename}"

    _log.info("☁️ Subiendo a Supabase Storage: %s (%d bytes)", storage_path, len(content))

    try:
        async with httpx.AsyncClient(timeout=180) as client:
            # Upload a Supabase Storage
            # Endpoint: /storage/v1/object/crm-media/{path}
            upload_url = f"{SUPABASE_URL}/storage/v1/object/crm-media/{storage_path}"

            r = await client.post(
                upload_url,
                headers={
                    "Authorization": f"Bearer {SUPABASE_KEY}",
                    "Content-Type": content_type,
                },
                content=content
            )

        if not r.is_success:
            error_detail = r.text
            _log.error("❌ Error subiendo a Supabase: status=%d, detail=%s", r.status_code, error_detail)
            raise Exception(f"Supabase Storage upload failed: {error_detail}")

        # Construir URL pública
        public_url = f"{STORAGE_BASE_URL}/{storage_path}"

        _log.info("✅ Archivo subido a Supabase: %s", public_url)
        return public_url

    except Exception as e:
        _log.error("❌ Error en _upload_to_supabase_storage: %s", str(e))
        raise


@router.post("/upload")
@limiter.limit("20/minute")
async def upload_media(request: Request, file: UploadFile = File(...), phone: str = Query(...)):
    """
    Sube un archivo a WhatsApp Media y Supabase Storage, retorna media_id y public_url.

    Args:
        file: Archivo a subir (imagen/video/audio)
        phone: Teléfono del cliente (para organizar en Supabase Storage)

    Returns:
        {
            "id": "media_id_de_whatsapp",
            "public_url": "https://...supabase.co/.../video.mp4"  # URL permanente para el CRM
        }
    """
    # ⚠️ CRÍTICO: No modificar la lógica de este endpoint sin autorización explícita.
    # Cambios aquí afectan el envío de media (fotos/videos/audio) desde el CRM.
    # Historial: se rompió el envío de media en sesiones anteriores al tocar este archivo.

    _log.info("📤 /wa/upload - Recibido: filename=%s, content_type=%s, phone=%s",
              file.filename, file.content_type, phone)

    content = await file.read()
    _log.info("📤 Archivo leído: %d bytes", len(content))

    base_type = (file.content_type or "application/octet-stream").split(";")[0].strip()

    if base_type.startswith("image/"):
        declared_category = "image"
        size_limit = MAX_SIZE_IMAGE
    elif base_type.startswith("video/"):
        declared_category = "video"
        size_limit = MAX_SIZE_VIDEO
    elif base_type.startswith("audio/"):
        declared_category = "audio"
        size_limit = MAX_SIZE_AUDIO
    else:
        declared_category = "document"
        size_limit = MAX_SIZE_DOCUMENT

    if len(content) > size_limit:
        _log.error("❌ /wa/upload - Archivo excede el límite: %d bytes (límite %d) tipo=%s",
                  len(content), size_limit, base_type)
        raise HTTPException(
            status_code=413,
            detail=f"Archivo demasiado grande ({len(content)} bytes). Límite para {base_type}: {size_limit} bytes",
        )

    real_category = _detect_real_category(content)
    if real_category != declared_category:
        _log.error("❌ /wa/upload - Tipo de archivo no coincide: declarado=%s (%s), detectado=%s",
                  base_type, declared_category, real_category)
        raise HTTPException(
            status_code=415,
            detail=f"El contenido del archivo no corresponde al tipo declarado ({base_type})",
        )

    upload_content = content
    upload_filename = file.filename or "upload"
    upload_type = base_type

    # NUEVO: Convertir videos a H.264/AAC para compatibilidad con WhatsApp
    if base_type == "video/mp4" or (file.filename and file.filename.lower().endswith('.mp4')):
        try:
            _log.info("🎬 Video detectado, convirtiendo a H.264/AAC...")
            upload_content, upload_filename = _convert_video_to_h264(content, file.filename)
            upload_type = "video/mp4"
            _log.info("🎬 Video convertido exitosamente")
        except Exception as conv_err:
            _log.error("❌ Error convirtiendo video, enviando original: %s", str(conv_err))
            # Si falla la conversión, intentar enviar el original
            # (mejor intentar con el original que fallar completamente)

    if base_type.startswith("audio/"):
        _log.info("🎵 Convirtiendo audio a opus...")
        upload_content, upload_filename = _convert_audio_to_opus(content)
        upload_type = "audio/ogg"
        _log.info("🎵 Audio convertido: %s (%d bytes)", upload_filename, len(upload_content))

    # ──────────────────────────────────────────────────────────────────────────
    # PASO 1: Subir a Supabase Storage (URL permanente para el CRM)
    # ──────────────────────────────────────────────────────────────────────────
    public_url = None
    try:
        public_url = await _upload_to_supabase_storage(
            content=upload_content,
            phone=phone,
            filename=upload_filename,
            content_type=upload_type
        )
        _log.info("✅ Supabase Storage URL: %s", public_url)
    except Exception as storage_err:
        _log.error("❌ Error subiendo a Supabase Storage: %s", str(storage_err))
        # Continuar con WhatsApp upload aunque falle Supabase
        # El CRM recibirá solo el media_id si esto falla

    # ──────────────────────────────────────────────────────────────────────────
    # PASO 2: Subir a WhatsApp Media API (media_id temporal para envío)
    # ──────────────────────────────────────────────────────────────────────────
    try:
        async with httpx.AsyncClient(timeout=180) as client:  # 3 minutos para videos pesados
            _log.info("📤 Subiendo a Meta: %s (%s, %d bytes)", upload_filename, upload_type, len(upload_content))
            r = await client.post(
                f"{_base()}/media",
                headers=_auth(),
                data={"messaging_product": "whatsapp", "type": upload_type},
                files={"file": (upload_filename, upload_content, upload_type)},
            )

        if not r.is_success:
            error_detail = r.json()
            _log.error("❌ /wa/upload - Error de Meta: status=%d, detail=%s", r.status_code, error_detail)
            raise HTTPException(status_code=r.status_code, detail=error_detail)

        result = r.json()
        media_id = result.get("id", "unknown")

        # ──────────────────────────────────────────────────────────────────────
        # PASO 3: Retornar media_id + public_url para el CRM
        # ──────────────────────────────────────────────────────────────────────
        response = {
            "id": media_id,
            "public_url": public_url  # URL permanente en Supabase Storage
        }

        _log.info("✅ /wa/upload - Éxito: media_id=%s, public_url=%s", media_id, public_url)
        return response

    except HTTPException:
        raise
    except Exception as e:
        _log.error("❌ /wa/upload - Excepción: %s", str(e), exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error subiendo archivo: {str(e)}")


@router.post("/media")
async def send_media(payload: MediaPayload):
    """Envía un mensaje de imagen, video o audio usando un media_id ya subido."""
    # ⚠️ CRÍTICO: No modificar la lógica de este endpoint sin autorización explícita.
    # Cambios aquí afectan el envío de media (fotos/videos/audio) desde el CRM.
    # Historial: se rompió el envío de media en sesiones anteriores al tocar este archivo.

    _log.info("📸 /wa/media - Enviando: to=%s, type=%s, media_id=%s, caption=%s",
              payload.to, payload.type, payload.media_id, payload.caption or "sin caption")

    # Validar que media_id no esté vacío
    if not payload.media_id or payload.media_id == "unknown":
        _log.error("❌ /wa/media - media_id inválido: %s", payload.media_id)
        raise HTTPException(status_code=400, detail="media_id es requerido y debe ser válido")

    media_obj: dict = {"id": payload.media_id}
    if payload.type == "audio":
        media_obj["voice"] = True
    if payload.caption:
        media_obj["caption"] = payload.caption
    if payload.type == "document" and payload.filename:
        media_obj["filename"] = payload.filename
    body = {
        "messaging_product": "whatsapp",
        "to": payload.to,
        "type": payload.type,
        payload.type: media_obj,
    }

    _log.info("📸 Payload completo: %s", body)

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                f"{_base()}/messages",
                headers={**_auth(), "Content-Type": "application/json"},
                json=body,
            )

        # Log de la respuesta completa de Meta (incluye errores silenciosos)
        response_data = r.json()
        _log.info("📸 Respuesta de Meta: status=%d, body=%s", r.status_code, response_data)

        if not r.is_success:
            _log.error("❌ /wa/media - Error de Meta: to=%s, status=%d, detail=%s",
                      payload.to, r.status_code, response_data)
            raise HTTPException(status_code=r.status_code, detail=response_data)

        result = response_data
        message_id = result.get("messages", [{}])[0].get("id", "unknown")
        _log.info("✅ /wa/media - Éxito: to=%s, type=%s, message_id=%s",
                 payload.to, payload.type, message_id)
        return result
    except HTTPException:
        raise
    except Exception as e:
        _log.error("❌ /wa/media - Excepción: to=%s, error=%s", payload.to, str(e), exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error enviando media: {str(e)}")
