import logging
import sys
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

load_dotenv()  # ← Cargar .env antes de importar wa_router

from limiter import limiter
from wa_router import router as wa_router

# ── Configuración de Logging ────────────────────────────────────────────────
# Configurar el logger raíz para que escriba a stdout (capturado por systemd)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(name)s %(levelname)s %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout)  # ← Esto va a /var/log/retrogaming/api.log
    ],
    force=True  # ← Forzar reconfiguración (importante para uvicorn)
)

# Configurar loggers específicos
logging.getLogger("uvicorn.access").setLevel(logging.WARNING)  # Reducir ruido de requests HTTP
logging.getLogger("httpx").setLevel(logging.WARNING)           # Reducir ruido de httpx

# Asegurar que wa_router use el mismo handler
logging.getLogger("wa_router").setLevel(logging.INFO)

app = FastAPI()

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://diegocourbis95.github.io"],
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type"],
)

app.include_router(wa_router)
