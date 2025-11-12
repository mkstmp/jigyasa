# backend/main.py
from __future__ import annotations

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Query, Request, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import RedirectResponse, JSONResponse
from starlette.websockets import WebSocketState
from typing import Dict, Any, List, Optional
import os, json, asyncio, contextlib, logging

from authlib.integrations.starlette_client import OAuth

from backend.shared.schemas import EventRecord, ProfileState
from backend.shared import config
from backend.agents import logger_agent
from backend.live.gemini_live import GeminiLiveBridge, LiveBridgeError
from backend.agents.tool_router import get_current_question_for_session

# -----------------------------------------------------------------------------
# App setup
# -----------------------------------------------------------------------------
app = FastAPI(title="Jigyāsa API (v4 - LIVE + OAuth)", version="0.5.0")

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("jigyasa")

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=config.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Cookie session for OAuth
app.add_middleware(SessionMiddleware, secret_key=config.AUTH_SECRET, same_site="lax", https_only=False)

# Static files
STATIC_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "static"))
os.makedirs(STATIC_DIR, exist_ok=True)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# -----------------------------------------------------------------------------
# OAuth (Google)
# -----------------------------------------------------------------------------
oauth = OAuth()
if config.GOOGLE_OAUTH_CLIENT_ID and config.GOOGLE_OAUTH_CLIENT_SECRET:
    oauth.register(
        name="google",
        server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
        client_id=config.GOOGLE_OAUTH_CLIENT_ID,
        client_secret=config.GOOGLE_OAUTH_CLIENT_SECRET,
        client_kwargs={"scope": "openid email profile"},
    )
else:
    log.warning("Google OAuth not configured. Set GOOGLE_OAUTH_CLIENT_ID/SECRET to enable login.")

# -----------------------------------------------------------------------------
# In-memory user→profiles & profile state (demo)
# -----------------------------------------------------------------------------
# USERS_PROFILES maps user_email -> list of {id,name,grade,age,language}
USERS_PROFILES: Dict[str, List[Dict[str, Any]]] = {}

PROFILE_STATE: Dict[str, Dict[str, Any]] = {}

def _get_state(profile_id: str) -> Dict[str, Any]:
    return PROFILE_STATE.setdefault(profile_id, {
        "profile_id": profile_id,
        "streak": 0,
        "weak_topics": ["numbers"],
        "seen_ids": [],
        "last_10": [],
        "attempted": 0,
        "correct": 0,
        "current_topic": "numbers",
        "list_sessions": {},
    })

# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------
def current_user(request: Request) -> Optional[Dict[str, Any]]:
    """Return user dict from session if logged in, else None."""
    return request.session.get("user")

def require_user(request: Request) -> Dict[str, Any]:
    u = current_user(request)
    if not u:
        raise HTTPException(status_code=401, detail="Login required")
    return u

def make_profile_id(user_email: str, name: str) -> str:
    safe = name.lower().strip().replace(" ", "_")
    return f"{safe}_{abs(hash(user_email + '|' + safe)) % (10**8)}"

# -----------------------------------------------------------------------------
# Events / profile state (unchanged)
# -----------------------------------------------------------------------------
@app.post("/events/record")
def record_event(ev: EventRecord):
    state = _get_state(ev.profile_id)
    state["last_10"].append({"event": ev.type})
    state["last_10"] = state["last_10"][-10:]
    logger_agent.log_event({
        "type": ev.type,
        "session_id": ev.session_id,
        "profile_id": ev.profile_id,
        "extra": ev.extra
    })
    return {"ok": True}

@app.get("/profiles/{profile_id}/state", response_model=ProfileState)
def profile_state(profile_id: str):
    st = _get_state(profile_id)
    weak = []
    if st["attempted"] > 0 and (st["correct"] / max(1, st["attempted"])) < 0.6:
        weak.append("numbers")
    return ProfileState(
        profile_id=profile_id,
        streak=st["streak"],
        weak_topics=weak or st.get("weak_topics", []),
        seen_ids=st["seen_ids"][-50:],
        last_10=st["last_10"],
        attempted=st["attempted"],
        correct=st["correct"],
    )

# -----------------------------------------------------------------------------
# Read-only sync: UI can poll the last question
# -----------------------------------------------------------------------------
@app.get("/live/current_question")
def http_get_current_question(session_id: str = Query(..., description="ProfileId acts as SessionId")):
    q = get_current_question_for_session(session_id)
    if not q:
        raise HTTPException(status_code=404, detail="no current question")
    q = {k: v for k, v in q.items() if k != "_answer"}
    return {"question": q}

# -----------------------------------------------------------------------------
# Auth routes
# -----------------------------------------------------------------------------
@app.get("/auth/login")
async def auth_login(request: Request):
    if "google" not in oauth:
        raise HTTPException(status_code=503, detail="OAuth not configured")
    redirect_uri = config.GOOGLE_OAUTH_REDIRECT_URI
    return await oauth.google.authorize_redirect(request, redirect_uri)

@app.get("/auth/callback")
async def auth_callback(request: Request):
    if "google" not in oauth:
        raise HTTPException(status_code=503, detail="OAuth not configured")
    token = await oauth.google.authorize_access_token(request)
    userinfo = token.get("userinfo") or {}
    # normalize minimal user
    user = {
        "email": userinfo.get("email"),
        "name": userinfo.get("name"),
        "picture": userinfo.get("picture"),
        "sub": userinfo.get("sub"),
    }
    if not user["email"]:
        raise HTTPException(status_code=400, detail="email not found in Google profile")
    request.session["user"] = user
    # if first login, seed empty profiles list
    USERS_PROFILES.setdefault(user["email"], [])
    # redirect to frontend (adjust to your FE origin if different)
    return RedirectResponse(url="/")

@app.post("/auth/logout")
async def auth_logout(request: Request):
    request.session.clear()
    return {"ok": True}

@app.get("/me")
async def me(request: Request):
    user = current_user(request)
    return {
        "user": user,
        "active_profile_id": request.session.get("active_profile_id")
    }

# -----------------------------------------------------------------------------
# Profiles API (CRUD-lite)
# -----------------------------------------------------------------------------
@app.get("/profiles")
async def list_profiles(user=Depends(require_user)):
    return {"profiles": USERS_PROFILES.get(user["email"], [])}

@app.post("/profiles")
async def create_profile(request: Request, user=Depends(require_user)):
    body = await request.json()
    name = (body.get("name") or "").strip()
    grade = body.get("grade") or "KG"
    age = int(body.get("age") or 5)
    language = body.get("language") or "en-IN"
    if not name:
        raise HTTPException(status_code=400, detail="name required")
    pid = make_profile_id(user["email"], name)
    prof = {"id": pid, "name": name, "grade": grade, "age": age, "language": language}
    lst = USERS_PROFILES.setdefault(user["email"], [])
    lst.append(prof)
    # optional: set first profile as active if none
    if not request.session.get("active_profile_id"):
        request.session["active_profile_id"] = pid
    return {"profile": prof}

@app.post("/profiles/select")
async def select_profile(request: Request, user=Depends(require_user)):
    body = await request.json()
    pid = body.get("profile_id")
    if not pid:
        raise HTTPException(status_code=400, detail="profile_id required")
    if pid not in {p["id"] for p in USERS_PROFILES.get(user["email"], [])}:
        raise HTTPException(status_code=404, detail="profile not found for user")
    request.session["active_profile_id"] = pid
    return {"ok": True, "active_profile_id": pid}

@app.delete("/profiles/{profile_id}")
async def delete_profile(profile_id: str, request: Request, user=Depends(require_user)):
    lst = USERS_PROFILES.get(user["email"], [])
    newlst = [p for p in lst if p["id"] != profile_id]
    if len(newlst) == len(lst):
        raise HTTPException(status_code=404, detail="profile not found")
    USERS_PROFILES[user["email"]] = newlst
    if request.session.get("active_profile_id") == profile_id:
        request.session["active_profile_id"] = newlst[0]["id"] if newlst else None
    return {"ok": True}

# -----------------------------------------------------------------------------
# Gemini LIVE WebSocket bridge (PCM path)
# -----------------------------------------------------------------------------
@app.websocket("/live/ws")
async def live_ws(ws: WebSocket):
    await ws.accept()
    # Prefer active profile in cookie session. Allow ?session_id= override for dev.
    try:
        # Access the ASGI scope to reach the same session as HTTP routes
        request = Request(ws.scope)
        active_pid = request.session.get("active_profile_id")
    except Exception:
        active_pid = None

    sid = (ws.query_params.get("session_id") or active_pid or "kid_demo_001").strip()
    peer = f"{ws.client.host}:{ws.client.port if ws.client else 'unknown'}"
    log.info("WS accepted from %s (session_id=%s)", peer, sid)

    async def safe_send_json(payload: dict):
        if ws.application_state != WebSocketState.CONNECTED:
            return
        with contextlib.suppress(Exception):
            await ws.send_json(payload)

    async def safe_send_bytes(b: bytes):
        if ws.application_state != WebSocketState.CONNECTED:
            return
        with contextlib.suppress(Exception):
            await ws.send_bytes(b)

    if not config.GOOGLE_API_KEY:
        await safe_send_json({"type": "error", "error": "NO_API_KEY"})
        with contextlib.suppress(Exception):
            await ws.close()
        log.error("WS %s closed: no API key", peer)
        return

    bridge = GeminiLiveBridge()
    # IMPORTANT: set session BEFORE start() to avoid races
    bridge.set_session(sid)
    # Bind downstream push paths
    bridge.bind_ui_sender(safe_send_json)
    bridge.bind_audio_sender(safe_send_bytes)

    try:
        await bridge.start()
        await safe_send_json({"type": "session", "session_id": sid})
        await safe_send_json({"type": "ready"})
        await safe_send_json({"type": "pcm_meta", "rate": bridge.out_rate_hz})

        # Browser mic/control loop
        mime = "audio/pcm;rate=16000"
        while True:
            msg = await ws.receive()

            if msg.get("type") == "websocket.disconnect":
                log.info("WS disconnected from %s", peer)
                break

            if isinstance(msg.get("text"), str):
                try:
                    obj = json.loads(msg["text"])
                except Exception:
                    continue
                t = obj.get("type")
                if t == "hello":
                    mime = obj.get("mime") or mime
                    await safe_send_json({"type": "ack", "mime": mime})
                elif t == "commit":
                    with contextlib.suppress(Exception):
                        await bridge.commit_segment()
                elif t == "done":
                    break
                elif t == "tool":
                    # optional: if you want to pass FE-triggered tools to the model
                    pass
                continue

            data = msg.get("bytes")
            if data:
                with contextlib.suppress(Exception):
                    await bridge.send_audio(data, mime)

    except WebSocketDisconnect:
        log.info("WS %s disconnected", peer)
    except Exception as e:
        log.exception("WS %s error: %s", peer, e)
    finally:
        with contextlib.suppress(Exception):
            await bridge.close()
        with contextlib.suppress(Exception):
            await ws.close()
        log.info("WS %s closed", peer)
