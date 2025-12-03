# backend/shared/config.py
from __future__ import annotations
import json, os, logging
from pathlib import Path

log = logging.getLogger("jigyasa.config")

# --- .env loading (robust) ---
try:
    from dotenv import load_dotenv, find_dotenv
    # Prefer explicit project-root .env if present; else fall back to auto-discovery
    repo_root = Path(__file__).resolve().parents[2]
    explicit = repo_root / ".env"
    env_path = str(explicit) if explicit.exists() else find_dotenv(usecwd=True)
    if env_path:
        load_dotenv(env_path)
        log.info("Loaded .env from %s", env_path)
    else:
        log.info("No .env found; using process env only.")
except Exception:
    # dotenv optional
    pass

def _get(name: str, default: str | None = None) -> str | None:
    v = os.getenv(name, default)
    if v is None:
        return None
    v = v.strip()
    return v if v != "" else default

def _get_json_list(name: str, default: list[str] | None = None) -> list[str]:
    raw = _get(name)
    if not raw:
        return list(default or [])
    # Accept JSON array OR comma-separated list
    try:
        val = json.loads(raw)
        if isinstance(val, list):
            return [str(x).strip() for x in val if str(x).strip()]
    except Exception:
        pass
    return [x.strip() for x in raw.split(",") if x.strip()]

def _get_bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return str(v).strip().lower() in ("1", "true", "yes", "on")


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


GCP_PROJECT_ID: str = _get("GCP_PROJECT_ID", "gen-lang-client-0749617334")

# Google GenAI / Gemini Live
GOOGLE_API_KEY: str | None = _get("GOOGLE_API_KEY")
GEMINI_LIVE_MODEL: str = _get("GEMINI_LIVE_MODEL", "gemini-2.5-flash-native-audio-preview-09-2025")

# Google OAuth
GOOGLE_OAUTH_CLIENT_ID: str | None = _get("GOOGLE_OAUTH_CLIENT_ID")
GOOGLE_OAUTH_CLIENT_SECRET: str | None = _get("GOOGLE_OAUTH_CLIENT_SECRET")
GOOGLE_OAUTH_REDIRECT_URI: str = _get("GOOGLE_OAUTH_REDIRECT_URI", "http://localhost:8000/auth/callback")

# Frontend
FRONTEND_ORIGIN: str = _get("FRONTEND_ORIGIN", "http://localhost:5173")

# Cookie flags (useful in prod)
SESSION_HTTPS_ONLY: bool = _get_bool("SESSION_HTTPS_ONLY", default=False)
SESSION_COOKIE_SAMESITE: str = _get("SESSION_COOKIE_SAMESITE", "lax")  # 'lax'|'strict'|'none'
SESSION_HTTPS_ONLY: bool = _get_bool("SESSION_HTTPS_ONLY", False)

def oauth_configured() -> bool:
    return bool(GOOGLE_OAUTH_CLIENT_ID and GOOGLE_OAUTH_CLIENT_SECRET)

# Debug (safe—does not print secrets)
log.info(
    "Config loaded | CORS_ORIGINS=%s, GEMINI_LIVE_MODEL=%s, OAuth=%s, Redirect=%s, API_KEY=%s",
    CORS_ORIGINS,
    GEMINI_LIVE_MODEL,
    oauth_configured(),
    GOOGLE_OAUTH_REDIRECT_URI,
    "set" if GOOGLE_API_KEY else "missing",
)
