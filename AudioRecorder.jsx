/**
 * AudioRecorder — graba audio, lo sube a Supabase Storage para obtener
 * una URL pública permanente, lo envía por WhatsApp y notifica a n8n.
 *
 * Uso:
 *   <AudioRecorder
 *     phone="5491112345678"
 *     mode="humano"
 *     n8nWebhookUrl="https://tu-n8n.com/webhook/audio"   ← opcional
 *     onSent={() => window.__crm_refresh?.()}
 *   />
 *
 * Dependencias globales esperadas (ya definidas en index.html):
 *   sb            — cliente Supabase
 *   WA_TOKEN      — token WhatsApp Business
 *   WA_PHONE_ID   — phone ID de WhatsApp Business
 *   showToast(msg, type, duration)
 */

const STORAGE_BUCKET = 'crm-media';

// Pasos visibles en la UI para dar feedback granular al usuario
const STEPS = {
  idle:       { icon: '🎤', label: 'Grabar audio' },
  recording:  { icon: '⏹',  label: 'Detener grabación' },
  uploading:  { icon: '⏳', label: 'Subiendo a Storage...' },
  sending_wa: { icon: '⏳', label: 'Enviando por WhatsApp...' },
  notifying:  { icon: '⏳', label: 'Notificando a n8n...' },
  saving:     { icon: '⏳', label: 'Guardando en BD...' },
  error:      { icon: '❌', label: 'Error — toca para reintentar' },
};

function AudioRecorder({ phone, mode, n8nWebhookUrl = null, onSent = null }) {
  const [step, setStep]         = React.useState('idle');
  const [errorMsg, setErrorMsg] = React.useState(null);
  const mediaRef                = React.useRef(null);
  const chunksRef               = React.useRef([]);
  const streamRef               = React.useRef(null);

  // Limpieza si el componente se desmonta durante una grabación
  React.useEffect(() => {
    return () => {
      streamRef.current?.getTracks().forEach(t => t.stop());
    };
  }, []);

  // ─── Paso 1: subir blob a Supabase Storage ───────────────────────────────
  async function uploadToStorage(blob) {
    const fileName = `audio/${Date.now()}-${phone}.ogg`;
    const { error } = await sb.storage
      .from(STORAGE_BUCKET)
      .upload(fileName, blob, { contentType: 'audio/ogg', upsert: false });

    if (error) throw new Error(`Storage: ${error.message}`);

    const { data } = sb.storage.from(STORAGE_BUCKET).getPublicUrl(fileName);
    if (!data?.publicUrl) throw new Error('Storage: no se pudo obtener la URL pública');

    return data.publicUrl; // URL permanente, accesible desde n8n y cualquier servidor
  }

  // ─── Paso 2: subir a WhatsApp Media API y enviar mensaje ─────────────────
  async function sendViaWhatsApp(blob, publicUrl) {
    // 2a. Subir el audio a la Media API de WhatsApp
    const fd = new FormData();
    fd.append('messaging_product', 'whatsapp');
    fd.append('file', blob, 'audio.ogg');
    fd.append('type', 'audio/ogg');

    const upRes = await fetch(
      `https://graph.facebook.com/v19.0/${WA_PHONE_ID}/media`,
      { method: 'POST', headers: { Authorization: `Bearer ${WA_TOKEN}` }, body: fd }
    );
    if (!upRes.ok) {
      const e = await upRes.json().catch(() => ({}));
      throw new Error(`WhatsApp Media API: ${e?.error?.message || upRes.status}`);
    }
    const { id: mediaId } = await upRes.json();

    // 2b. Enviar el mensaje de audio al cliente
    const sendRes = await fetch(
      `https://graph.facebook.com/v19.0/${WA_PHONE_ID}/messages`,
      {
        method: 'POST',
        headers: { Authorization: `Bearer ${WA_TOKEN}`, 'Content-Type': 'application/json' },
        body: JSON.stringify({
          messaging_product: 'whatsapp',
          to: phone,
          type: 'audio',
          audio: { id: mediaId },
        }),
      }
    );
    if (!sendRes.ok) {
      const e = await sendRes.json().catch(() => ({}));
      throw new Error(`WhatsApp Messages API: ${e?.error?.message || sendRes.status}`);
    }
  }

  // ─── Paso 3: notificar al webhook de n8n con la URL pública ──────────────
  async function notifyN8n(publicUrl) {
    if (!n8nWebhookUrl) return; // opcional — se omite si no se configura

    const res = await fetch(n8nWebhookUrl, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      // n8n recibe un JSON con toda la info necesaria para procesar el audio
      body: JSON.stringify({
        event:      'audio_sent',
        phone,
        audio_url:  publicUrl,          // URL pública permanente, no blob:
        timestamp:  new Date().toISOString(),
      }),
    });

    // No lanzamos error si n8n falla: el audio ya fue enviado; solo logueamos
    if (!res.ok) {
      console.warn(`[AudioRecorder] n8n webhook respondió ${res.status} — audio ya enviado igual.`);
    }
  }

  // ─── Paso 4: guardar en Supabase conversaciones ───────────────────────────
  async function saveToDb(publicUrl) {
    const { error } = await sb.from('conversaciones').insert({
      phone,
      rol:        'humano',
      mensaje:    '[AUDIO]',
      media_url:  publicUrl,   // ← URL pública, no blob: — accesible desde n8n
      media_type: 'audio/ogg',
      leido:      true,
    });
    if (error) throw new Error(`Supabase insert: ${error.message}`);
  }

  // ─── Orquestador principal (se ejecuta al detener la grabación) ───────────
  async function processAudio(blob) {
    try {
      setErrorMsg(null);

      setStep('uploading');
      const publicUrl = await uploadToStorage(blob);

      setStep('sending_wa');
      await sendViaWhatsApp(blob, publicUrl);

      setStep('notifying');
      await notifyN8n(publicUrl);

      setStep('saving');
      await saveToDb(publicUrl);

      setStep('idle');
      showToast('AUDIO ENVIADO', 'success');
      onSent?.();
    } catch (err) {
      console.error('[AudioRecorder]', err);
      setStep('error');
      setErrorMsg(err.message);
      showToast(`ERROR: ${err.message}`, 'error', 6000);
    }
  }

  // ─── Toggle grabar / detener ──────────────────────────────────────────────
  async function toggleRecord() {
    if (mode !== 'humano') return;

    if (step === 'error') {
      // Reinicio manual tras error
      setStep('idle');
      setErrorMsg(null);
      return;
    }

    if (step === 'recording') {
      mediaRef.current?.stop(); // dispara onstop → processAudio
      return;
    }

    if (step !== 'idle') return; // bloquear si hay operación en curso

    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      streamRef.current = stream;

      const mr = new MediaRecorder(stream);
      chunksRef.current = [];

      mr.ondataavailable = e => {
        if (e.data.size > 0) chunksRef.current.push(e.data);
      };

      mr.onstop = () => {
        stream.getTracks().forEach(t => t.stop());
        streamRef.current = null;
        const blob = new Blob(chunksRef.current, { type: 'audio/ogg; codecs=opus' });
        processAudio(blob);
      };

      mr.start();
      mediaRef.current = mr;
      setStep('recording');
    } catch {
      showToast('ERROR: Permite acceso al micrófono', 'error', 4000);
    }
  }

  // ─── Estilos dinámicos según estado ──────────────────────────────────────
  const isRecording = step === 'recording';
  const isBusy      = ['uploading', 'sending_wa', 'notifying', 'saving'].includes(step);
  const isError     = step === 'error';
  const isDisabled  = mode !== 'humano' || isBusy;

  const bg     = isRecording ? 'var(--red-bg)'  : isError ? 'var(--red-bg)'  : isBusy ? 'var(--bg3)'  : 'var(--green-bg)';
  const color  = isRecording ? 'var(--red)'     : isError ? 'var(--red)'     : isBusy ? 'var(--text3)': 'var(--green)';
  const border = isRecording ? 'var(--red)'     : isError ? 'var(--red)'     : isBusy ? 'var(--border2)':'var(--green)';

  const currentStep = STEPS[step] ?? STEPS.idle;

  return (
    <button
      onClick={toggleRecord}
      disabled={isDisabled}
      title={errorMsg ?? currentStep.label}
      aria-label={currentStep.label}
      style={{
        background:    bg,
        color,
        border:        `2px solid ${border}`,
        borderRadius:  4,
        boxShadow:     `2px 2px 0 ${border}`,
        width:         40,
        height:        40,
        cursor:        isDisabled ? 'not-allowed' : 'pointer',
        fontSize:      17,
        flexShrink:    0,
        transition:    'all .2s',
        display:       'flex',
        alignItems:    'center',
        justifyContent:'center',
        opacity:       isDisabled && !isRecording ? 0.35 : 1,
        animation:     isRecording ? 'pulse 1s infinite' : '',
      }}
    >
      {currentStep.icon}
    </button>
  );
}
