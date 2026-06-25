# Fix: Videos de WhatsApp → Supabase Storage

## Problema Identificado

Los videos recibidos por WhatsApp en el CRM:
1. ✅ Se convertían correctamente con ffmpeg (H.264/AAC)
2. ✅ Se subían a WhatsApp Media API (media_id temporal)
3. ❌ NO se subían a Supabase Storage
4. ❌ El CRM guardaba `URL.createObjectURL()` (blob URL temporal del navegador)
5. ❌ Al recargar el CRM, la blob URL desaparecía → video player negro

## Solución Implementada

### 1. Backend: Agregar variables de entorno (backend/.env)

Se agregaron las variables necesarias para Supabase Storage:
```bash
SUPABASE_URL=https://txbtmshmtustcrtpjqaj.supabase.co
SUPABASE_KEY=eyJhbGc...
STORAGE_BASE_URL=https://txbtmshmtustcrtpjqaj.supabase.co/storage/v1/object/public/crm-media
```

### 2. Backend: Nueva función de upload a Supabase (wa_router.py:165-203)

Se agregó la función `_upload_to_supabase_storage()`:

```python
async def _upload_to_supabase_storage(content: bytes, phone: str, filename: str, content_type: str) -> str:
    """
    Sube un archivo (video/imagen/audio) a Supabase Storage y retorna la URL pública.
    
    Path generado: chats/{phone}/{timestamp}_{filename}
    Ejemplo: chats/56912345678/20260609_153045_video.mp4
    
    Returns:
        URL pública permanente: https://...supabase.co/storage/v1/object/public/crm-media/chats/...
    """
```

**Características:**
- ✅ Path único con timestamp para evitar colisiones
- ✅ Organizado por teléfono del cliente (carpeta `chats/{phone}/`)
- ✅ Timeout de 180 segundos para videos pesados
- ✅ Logging detallado de errores
- ✅ Retorna URL pública permanente

### 3. Backend: Modificar endpoint /wa/upload (wa_router.py:206-287)

El endpoint ahora hace **2 uploads en paralelo**:

**PASO 1: Upload a Supabase Storage (URL permanente para el CRM)**
```python
public_url = await _upload_to_supabase_storage(
    content=upload_content,  # Video ya convertido con ffmpeg
    phone=phone,
    filename=upload_filename,
    content_type=upload_type
)
```

**PASO 2: Upload a WhatsApp Media API (media_id temporal para envío)**
```python
r = await client.post(
    f"{_base()}/media",
    headers=_auth(),
    data={"messaging_product": "whatsapp", "type": upload_type},
    files={"file": (upload_filename, upload_content, upload_type)},
)
```

**PASO 3: Retornar ambos valores al CRM**
```python
return {
    "id": media_id,          # Para envío por WhatsApp
    "public_url": public_url # Para guardar en BD (permanente)
}
```

**Cambios en la firma del endpoint:**
- Antes: `async def upload_media(file: UploadFile = File(...))`
- Ahora: `async def upload_media(file: UploadFile = File(...), phone: str = Query(...))`

El parámetro `phone` se pasa por query string: `/wa/upload?phone=56912345678`

### 4. Frontend: Usar public_url en vez de blob (index.html)

**Cambio 1: Función enviarMedia() - Línea 226-228**
```javascript
// ANTES:
await sb.from('conversaciones').insert({
    phone: mediaActivePhone,
    rol: 'humano',
    mensaje: caption||'[MEDIA]',
    media_url: URL.createObjectURL(archivoActual),  // ❌ Blob temporal
    media_type: archivoActual.type,
    leido: true
});

// AHORA:
var finalMediaUrl = upD.public_url || URL.createObjectURL(archivoActual);
await sb.from('conversaciones').insert({
    phone: mediaActivePhone,
    rol: 'humano',
    mensaje: caption||'[MEDIA]',
    media_url: finalMediaUrl,  // ✅ URL permanente de Supabase
    media_type: archivoActual.type,
    leido: true
});
```

**Cambio 2: Función archivoSeleccionado() - Línea 149-167**
```javascript
// ANTES: Hacía doble upload (a Meta + manualmente a Supabase)
var storageUrl2 = null;
try {
    var upSB2 = await sb.storage.from('crm-media').upload(fileName2, file, ...);
    if (!upSB2.error) {
        var pubUrl2 = sb.storage.from('crm-media').getPublicUrl(fileName2);
        storageUrl2 = pubUrl2.data && pubUrl2.data.publicUrl || null;
    }
} catch(e4) { console.error('Storage error:', e4); }
var finalUrl = storageUrl2 || URL.createObjectURL(file);

// AHORA: Usa directamente la URL del backend
var finalUrl = upD.public_url || URL.createObjectURL(file);
```

## Flujo Completo

```
Usuario arrastra video al CRM
    ↓
CRM: POST /wa/upload?phone=56912345678
    ↓
Backend:
    1. Lee video (bytes)
    2. Convierte con ffmpeg a H.264/AAC
    3. Sube a Supabase Storage → public_url
    4. Sube a WhatsApp Media API → media_id
    5. Retorna: { "id": "...", "public_url": "https://..." }
    ↓
CRM: Envía por WhatsApp usando media_id
    ↓
CRM: Guarda en BD usando public_url
    ↓
✅ Video disponible permanentemente en el CRM
```

## Bucket de Supabase Storage

**Bucket:** `crm-media`

**Estructura de carpetas:**
```
crm-media/
├── chats/
│   ├── 56912345678/
│   │   ├── 20260609_153045_video.mp4
│   │   ├── 20260609_153120_imagen.jpg
│   │   └── 20260609_153200_audio.ogg
│   └── 56987654321/
│       └── 20260609_154000_video.mp4
├── flyers/
│   ├── k8.jpg
│   └── r36s.jpg
└── catalogo/
    └── ...
```

**Políticas RLS:**
- El bucket `crm-media` debe tener políticas que permitan:
  - ✅ INSERT público (para que el backend pueda subir)
  - ✅ SELECT público (para que el CRM pueda ver los videos)

Si las políticas no existen, ejecutar:
```sql
-- Permitir INSERT desde el backend
CREATE POLICY "Allow public uploads to crm-media"
ON storage.objects FOR INSERT
WITH CHECK (bucket_id = 'crm-media');

-- Permitir SELECT público (URLs públicas)
CREATE POLICY "Allow public access to crm-media"
ON storage.objects FOR SELECT
USING (bucket_id = 'crm-media');
```

## Testing

### Test Manual 1: Video desde CRM

1. Abrir CRM en el navegador
2. Seleccionar un chat activo
3. Hacer clic en "Adjuntar media"
4. Arrastrar un video MP4 (H.265 o cualquier codec)
5. Enviar el video
6. **Verificar:**
   - ✅ El video se envía por WhatsApp correctamente
   - ✅ En la tabla `conversaciones`, `media_url` tiene una URL de Supabase (no blob:)
   - ✅ Al hacer clic en el video en el CRM, se reproduce correctamente
   - ✅ Al recargar el CRM, el video sigue funcionando

### Test Manual 2: Verificar Supabase Storage

1. Ir a Supabase Dashboard → Storage → `crm-media`
2. Navegar a `chats/{phone}/`
3. **Verificar:**
   - ✅ El video está almacenado con nombre `{timestamp}_{filename}.mp4`
   - ✅ Se puede descargar y reproducir
   - ✅ La URL pública funciona

### Test de Logs

Buscar en logs del backend:
```bash
cd /root/retrogaming-crm/backend
tail -f logs.txt | grep -E "(🎬|☁️|✅)"
```

Deberías ver:
```
🎬 Convirtiendo video a H.264: 2456789 bytes
✅ Video convertido: 2456789 bytes → 1987654 bytes (80.9% del original)
☁️ Subiendo a Supabase Storage: chats/56912345678/20260609_153045_video.mp4 (1987654 bytes)
✅ Archivo subido a Supabase: https://...supabase.co/.../video.mp4
📤 Subiendo a Meta: video.mp4 (video/mp4, 1987654 bytes)
✅ /wa/upload - Éxito: media_id=123456789, public_url=https://...
```

## Rollback

Si algo falla, revertir cambios:

**Backend:**
```bash
cd /root/retrogaming-crm/backend
git checkout wa_router.py .env
```

**Frontend:**
```bash
cd /root/retrogaming-crm
git checkout index.html
```

## Archivos Modificados

### Backend
- ✅ `backend/.env` → Agregar variables SUPABASE_URL, SUPABASE_KEY, STORAGE_BASE_URL
- ✅ `backend/wa_router.py` → Nueva función + modificar endpoint /wa/upload

### Frontend
- ✅ `index.html` → Usar `public_url` en vez de blob URL (2 ubicaciones)

## Beneficios

1. ✅ **Videos permanentes**: No se pierden al recargar el CRM
2. ✅ **Reproducción confiable**: URL pública estable de Supabase
3. ✅ **Organización**: Videos organizados por cliente en Storage
4. ✅ **Auditoría**: Se puede ver en Supabase qué videos se enviaron
5. ✅ **Backup**: Videos quedan respaldados en Supabase
6. ✅ **Sin cambios en UX**: El usuario no nota la diferencia

## Próximos Pasos (Opcional)

1. **Limpieza automática**: Agregar cron job para borrar videos antiguos (>30 días)
2. **Compresión**: Agregar nivel de compresión configurable en ffmpeg
3. **Thumbnail**: Generar thumbnail del video para preview en el CRM
4. **Progress bar**: Mostrar progreso real del upload a Supabase

---

**Fecha de implementación:** 2026-06-09  
**Versión:** v1.0  
**Estado:** ✅ Implementado
