"""Configuración del agente. Todo lo ajustable vive acá."""

import os


def _int(name: str, default: int) -> int:
    """Lee una variable de entorno como entero, con valor por defecto."""
    return int(os.environ.get(name, default))


# --- Ventana de análisis ---
# 14 días: mayor o igual al staffing lead time (1-3 semanas para cubrir un rol).
WINDOW_DAYS = _int("WINDOW_DAYS", 14)

# --- Política de reintentos ---
MAX_ATTEMPTS = _int("MAX_ATTEMPTS", 4)      # ~99% de éxito con 30% de falla
RUN_BUDGET_SECONDS = _int("RUN_BUDGET_SECONDS", 120)
BACKOFF_BASE_SECONDS = 1.0                   # 1s, 2s, 4s

# --- Umbrales de confianza ---
CONFIDENCE_ASSERT = 0.9    # >= afirma
CONFIDENCE_ASK = 0.5       # entre ASK y ASSERT: pregunta
# < CONFIDENCE_ASK: reporta incertidumbre, no afirma

# --- Recordatorios ---
REMINDER_DAYS = _int("REMINDER_DAYS", 7)
RESOLUTION_TTL_DAYS = _int("RESOLUTION_TTL_DAYS", 90)

# --- Endpoints y credenciales ---
API_BASE_URL = os.environ.get("API_BASE_URL", "http://localhost:8000")
CANDIDATE_TOKEN = os.environ.get("CANDIDATE_TOKEN", "")
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")
DATABASE_URL = os.environ.get("DATABASE_URL", "")

# Dead man's switch: el agente pinguea esta URL al terminar bien.
# Si el ping no llega a horario, el servicio externo (ej. Healthchecks.io) avisa.
HEARTBEAT_URL = os.environ.get("HEARTBEAT_URL", "")

# --- Fuentes críticas vs enriquecedoras ---
# Si falla una crítica, el agente se calla.
CRITICAL_SOURCES = ("projects", "allocations", "time_off", "users")
ENRICHING_SOURCES = ("tasks", "members", "opportunities", "accounts", "time_entries")
