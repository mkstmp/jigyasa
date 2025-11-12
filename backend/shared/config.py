# backend/shared/config.py
from __future__ import annotations
import json, os, logging
from pathlib import Path

# Optional: load variables from a .env file at project root
try:
    from dotenv import load_dotenv
    # Look for .env in repo root (two levels up from this file)
    ROOT = Path(__file__).resolve().parents[2]
    load_dotenv(ROOT / ".env")
except Exception:
    # dotenv is optional; if missing, we just rely on process env
    pass

log = logging.getLogger("jigyasa.config")

def _get(name: str, default: str | None = None) -> str | None:
    v = os.getenv(name, default)
    if v is None:
        return None
    v = v.strip()
    return v if v != "" else default

def _get_json_list(name: str, default: list[str] | None = None) -> list[str]:
    raw = _get(name)
    if not raw:
        return default or []
    # Accept JSON array OR comma-separated list
    try:
        val = json.loads(raw)
        if isinstance(val, list):
            return [str(x) for x in val]
    except Exception:
        pass
    # fallback: comma separated
    return [x.strip() for x in raw.split(",") if x.strip()]

# -----------------------------------------------------------------------------
# Public config values used across the app
# -----------------------------------------------------------------------------

# FastAPI CORS
CORS_ORIGINS: list[str] = _get_json_list(
    "CORS_ORIGINS",
    default=["http://localhost:5173", "http://localhost:8000"],
)

# Cookie/session secret (Starlette SessionMiddleware)
AUTH_SECRET: str = _get("AUTH_SECRET", "dev-secret-change-me")

# Google GenAI / Gemini Live
GOOGLE_API_KEY: str | None = _get("GOOGLE_API_KEY")
GEMINI_LIVE_MODEL: str = _get("GEMINI_LIVE_MODEL", "gemini-2.5-flash-native-audio-preview-09-2025")

# Google OAuth
GOOGLE_OAUTH_CLIENT_ID: str | None = _get("GOOGLE_OAUTH_CLIENT_ID")
GOOGLE_OAUTH_CLIENT_SECRET: str | None = _get("GOOGLE_OAUTH_CLIENT_SECRET")
GOOGLE_OAUTH_REDIRECT_URI: str = _get("GOOGLE_OAUTH_REDIRECT_URI", "http://localhost:8000/auth/callback")

# Debug log of what we loaded (safe—does not print secrets)
log.info(
    "Config loaded | CORS_ORIGINS=%s, GEMINI_LIVE_MODEL=%s, "
    "OAuth=%s, Redirect=%s, API_KEY=%s",
    CORS_ORIGINS,
    GEMINI_LIVE_MODEL,
    bool(GOOGLE_OAUTH_CLIENT_ID) and bool(GOOGLE_OAUTH_CLIENT_SECRET),
    GOOGLE_OAUTH_REDIRECT_URI,
    "set" if GOOGLE_API_KEY else "missing",
)
