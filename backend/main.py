# backend/main.py
from __future__ import annotations

from fastapi import (
    FastAPI,
    WebSocket,
    WebSocketDisconnect,
    HTTPException,
    Query,
    Request,
    Depends,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, HTMLResponse
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import RedirectResponse
from starlette.websockets import WebSocketState
from typing import Dict, Any, Optional
import os, json, asyncio, contextlib, logging

from authlib.integrations.starlette_client import OAuth

from backend.content import curriculum
from backend.shared.schemas import EventRecord, ProfileState
from backend.shared import config
from backend.agents import logger_agent
from backend.live.gemini_live import GeminiLiveBridge, LiveBridgeError
from backend.agents.tool_router import (
    get_current_question_for_session,
    reset_session_state,
)

from backend.shared.profile_store import FirestoreProfileStore


# -----------------------------------------------------------------------------
# App setup
# -----------------------------------------------------------------------------
app = FastAPI(title="Jigyāsa API (v4 - LIVE + OAuth)", version="0.6.1")

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
app.add_middleware(
    SessionMiddleware,
    secret_key=config.AUTH_SECRET,
    same_site=config.SESSION_COOKIE_SAMESITE,
    https_only=config.SESSION_HTTPS_ONLY,
)

# Static files
STATIC_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "static"))
os.makedirs(STATIC_DIR, exist_ok=True)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# Serve the SPA at "/"
@app.get("/", response_class=HTMLResponse)
def serve_index():
    index_path = os.path.join(STATIC_DIR, "index.html")
    if not os.path.exists(index_path):
        raise HTTPException(status_code=500, detail="index.html not found in /static")
    return FileResponse(index_path)


# SPA fallback so client-side routing works
@app.middleware("http")
async def spa_fallback(request: Request, call_next):
    response = await call_next(request)
    if response.status_code == 404 and request.method in ("GET", "HEAD"):
        path = request.url.path
        if not (
            path.startswith("/auth")
            or path.startswith("/live/ws")
            or path.startswith("/static")
            or path.startswith("/docs")
            or path.startswith("/openapi.json")
        ):
            index_path = os.path.join(STATIC_DIR, "index.html")
            if os.path.exists(index_path):
                return FileResponse(index_path)
    return response


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
    log.warning(
        "Google OAuth not configured. Set GOOGLE_OAUTH_CLIENT_ID/SECRET to enable login."
    )

# Firestore-backed profiles; in-memory profile state (for now)
profile_store = FirestoreProfileStore()
PROFILE_STATE: Dict[str, Dict[str, Any]] = {}


def _get_state(profile_id: str) -> Dict[str, Any]:
    return PROFILE_STATE.setdefault(
        profile_id,
        {
            "profile_id": profile_id,
            "streak": 0,
            "weak_topics": ["numbers"],
            "seen_ids": [],
            "last_10": [],
            "attempted": 0,
            "correct": 0,
            "current_topic": "numbers",
            "list_sessions": {},
        },
    )


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
# Events (soft analytics / UI logs)
# -----------------------------------------------------------------------------
@app.post("/events/record")
def record_event(ev: EventRecord, request: Request):
    """
    Central event ingest for *soft* analytics (UI events, hints, etc).

    Gradebook (attempts, streaks, etc.) is handled in ToolRouter.update_answer
    via FirestoreProfileStore, not here.
    """
    user = current_user(request)

    # Log raw event (with user context if available) to top-level 'events' collection
    raw_log = ev.dict()
    if user:
        raw_log["user_email"] = user["email"]
    logger_agent.log_event(raw_log)

    return {"ok": True}


@app.get("/profiles/{profile_id}/state", response_model=ProfileState)
def profile_state(profile_id: str):
    # Still using in-memory state for now; can later be hydrated from Firestore
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
def http_get_current_question(
    session_id: str = Query(..., description="Session ID (same as WS session_id)"),
):
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
    if not (config.GOOGLE_OAUTH_CLIENT_ID and config.GOOGLE_OAUTH_CLIENT_SECRET):
        raise HTTPException(status_code=503, detail="OAuth not configured")
    client = oauth.create_client("google")
    if client is None:
        raise HTTPException(status_code=503, detail="OAuth client not registered")
    redirect_uri = config.GOOGLE_OAUTH_REDIRECT_URI
    return await client.authorize_redirect(request, redirect_uri)


@app.get("/auth/callback")
async def auth_callback(request: Request):
    if not (config.GOOGLE_OAUTH_CLIENT_ID and config.GOOGLE_OAUTH_CLIENT_SECRET):
        raise HTTPException(status_code=503, detail="OAuth not configured")
    client = oauth.create_client("google")
    if client is None:
        raise HTTPException(status_code=503, detail="OAuth client not registered")

    token = await client.authorize_access_token(request)

    userinfo = token.get("userinfo")
    if not userinfo:
        try:
            userinfo = await client.parse_id_token(request, token)
        except Exception:
            userinfo = {}

    user = {
        "email": userinfo.get("email"),
        "name": userinfo.get("name"),
        "picture": userinfo.get("picture"),
        "sub": userinfo.get("sub"),
    }
    if not user["email"]:
        raise HTTPException(status_code=400, detail="email not found in Google profile")

    # Persist basic user info in Firestore
    profile_store.ensure_user(
        email=user["email"],
        name=user["name"],
        picture=user["picture"],
    )

    request.session["user"] = user
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
        "active_profile_id": request.session.get("active_profile_id"),
    }


# -----------------------------------------------------------------------------
# Profiles API (CRUD-lite, Firestore-backed)
# -----------------------------------------------------------------------------
@app.get("/profiles")
async def list_profiles(user=Depends(require_user)):
    profiles = profile_store.list_profiles(user["email"])
    return {"profiles": profiles}


@app.post("/profiles")
async def create_profile(request: Request, user=Depends(require_user)):
    body = await request.json()
    name = (body.get("name") or "").strip()
    grade = body.get("grade") or "KG"
    age_val = body.get("age")
    age = int(age_val) if age_val not in (None, "") else 5
    language = body.get("language") or "en-IN"
    avatar = body.get("avatar") or ""

    if not name:
        raise HTTPException(status_code=400, detail="name required")

    pid = make_profile_id(user["email"], name)

    prof = {
        "id": pid,
        "name": name,
        "grade": grade,
        "age": age,
        "language": language,
        "avatar": avatar,
        "stats": {
            "attempted": 0,
            "correct": 0,
            "streak": 0,
        },
    }

    profile_store.upsert_profile(user["email"], prof)

    if not request.session.get("active_profile_id"):
        request.session["active_profile_id"] = pid

    return {"profile": prof}


@app.post("/profiles/select")
async def select_profile(request: Request, user=Depends(require_user)):
    body = await request.json()
    pid = (body.get("profile_id") or "").strip()
    if not pid:
        raise HTTPException(status_code=400, detail="profile_id required")

    prof = profile_store.get_profile(user["email"], pid)
    if not prof:
        raise HTTPException(status_code=404, detail="profile not found for user")

    request.session["active_profile_id"] = pid
    return {"ok": True, "active_profile_id": pid}


@app.delete("/profiles/{profile_id}")
async def delete_profile(profile_id: str, request: Request, user=Depends(require_user)):
    ok = profile_store.delete_profile(user["email"], profile_id)
    if not ok:
        raise HTTPException(status_code=404, detail="profile not found")

    if request.session.get("active_profile_id") == profile_id:
        request.session["active_profile_id"] = None

    return {"ok": True}


@app.get("/profiles/{profile_id}/lessons/{lesson_id}/stats")
async def get_lesson_stats(
    profile_id: str,
    lesson_id: str,
    user=Depends(require_user),
):
    """
    Return lifetime stats for a given lesson card for this profile:

        - attempted: total attempts across all sessions
        - correct:   total correct answers
        - mastered:  bool flag from Firestore
        - accuracy:  correct / attempted (0.0–1.0)
    """
    prof = profile_store.get_profile(user["email"], profile_id)
    if not prof:
        raise HTTPException(status_code=404, detail="profile not found")

    lesson_stats_map = prof.get("lesson_stats", {}) or {}
    raw = lesson_stats_map.get(lesson_id, {}) or {}

    attempted = int(raw.get("attempted", 0))
    correct = int(raw.get("correct", 0))
    mastered = bool(raw.get("mastered", False))
    accuracy = (correct / attempted) if attempted > 0 else 0.0

    return {
        "lesson_id": lesson_id,
        "attempted": attempted,
        "correct": correct,
        "mastered": mastered,
        "accuracy": accuracy,
    }


# -----------------------------------------------------------------------------
# Gemini LIVE WebSocket bridge (PCM path)
# -----------------------------------------------------------------------------
@app.websocket("/live/ws")
async def live_ws(ws: WebSocket):
    await ws.accept()

    # Prefer active profile in cookie session. Allow ?session_id= override for dev.
    try:
        request = Request(ws.scope)
        sess_user = request.session.get("user")
        parent_email = (sess_user or {}).get("email")
        active_pid = request.session.get("active_profile_id")
    except Exception:
        parent_email = None
        active_pid = None

    sid = (ws.query_params.get("session_id") or active_pid or "kid_demo_001").strip()
    peer = f"{ws.client.host}:{ws.client.port if ws.client else 'unknown'}"
    log.info(
        "WS accepted from %s (session_id=%s, profile_id=%s, parent=%s)",
        peer,
        sid,
        active_pid,
        parent_email,
    )
    # Fresh in-memory session for this live run (per WebSocket).
    # This keeps progress_log/seq per run so lesson_done is computed correctly.
    reset_session_state(sid)


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
    bridge.set_session(sid)
    bridge.bind_ui_sender(safe_send_json)
    bridge.bind_audio_sender(safe_send_bytes)

    # Build kid/profile context for this live session (if logged in)
    kid = None
    if parent_email and active_pid:
        try:
            prof = profile_store.get_profile(parent_email, active_pid)
        except Exception:
            prof = None

        if prof:
            kid = {
                "name": prof.get("name") or "Friend",
                "age": str(prof.get("age") or ""),
                "grade": prof.get("grade") or "",
                "language": prof.get("language") or "English",
            }

    # Initial context from server session (can be refined by 'hello' later)
    bridge.set_profile_context(
        parent_email=parent_email,
        profile_id=active_pid,
        kid=kid,
    )

    try:
        await bridge.start()
        await safe_send_json({"type": "session", "session_id": sid})
        await safe_send_json({"type": "ready"})
        await safe_send_json({"type": "pcm_meta", "rate": bridge.out_rate_hz})

        # IMPORTANT: don't send audio to Gemini until we know the mime / rate.
        mime: Optional[str] = None
        have_hello = False

        while True:
            msg = await ws.receive()

            if msg.get("type") == "websocket.disconnect":
                log.info("WS disconnected from %s", peer)
                break

            # Text frames (JSON control messages)
            if isinstance(msg.get("text"), str):
                try:
                    obj = json.loads(msg["text"])
                except Exception:
                    continue

                t = obj.get("type")
                # backend/main.py

                if t == "hello":
                    # Frontend hello can refine/override context
                    mime = obj.get("mime") or "audio/pcm;rate=16000"
                    have_hello = True

                    fe_parent = obj.get("parent_email") or parent_email
                    fe_profile = obj.get("profile_id") or active_pid or sid
                    fe_kid = obj.get("kid") or kid

                    # Update our locals and bridge context
                    parent_email = fe_parent
                    active_pid = fe_profile
                    kid = fe_kid

                    bridge.set_profile_context(
                        parent_email=parent_email,
                        profile_id=active_pid,
                        kid=kid,
                    )

                    # --- FIX: Pass the Lesson Context to the Bridge! ---
                    bridge.set_lesson_context(
                        grade=obj.get("grade"),
                        subject=obj.get("subject"),
                        topic=obj.get("topic"),
                        lesson_id=obj.get("lesson_id")
                    )
                    # ---------------------------------------------------

                    await safe_send_json({"type": "ack", "mime": mime})
                    # --- NEW: Kickstart the AI! ---
                    # This forces the AI to speak first without the user saying "Hello"
                    await bridge.send_text(
                        "The user is ready. Greet them warmly and IMMEDIATELY call get_question() to start."
                    )
                    # ------------------------------

                elif t == "commit":
                    with contextlib.suppress(Exception):
                        await bridge.commit_segment()
                elif t == "done":
                    break
                elif t == "tool":
                    # reserved if you want FE-driven tools later
                    pass

                continue  # move to next WS message

            # Binary frames (raw PCM audio)
            data = msg.get("bytes")
            if data:
                # Drop any audio that arrives before we've seen 'hello'
                if not have_hello:
                    log.debug(
                        "Dropping audio chunk before hello/config; len=%d",
                        len(data),
                    )
                    continue

                with contextlib.suppress(Exception):
                    await bridge.send_audio(data, mime)

    except WebSocketDisconnect:
        log.info("WS %s disconnected", peer)
    except LiveBridgeError as e:
        log.error("Live bridge error for %s: %s", peer, e)
        with contextlib.suppress(Exception):
            await safe_send_json({"type": "error", "error": str(e)})
    except Exception as e:
        log.exception("WS %s error: %s", peer, e)
    finally:
        with contextlib.suppress(Exception):
            await bridge.close()
        with contextlib.suppress(Exception):
            await ws.close()
        log.info("WS %s closed", peer)


@app.get("/curriculum")
async def get_curriculum(
    request: Request,
    grade: str = "KG",
    subject: str = "Math",
    profile_id: Optional[str] = None,
):
    """
    Return the list of lesson cards for a given grade+subject, annotated with
    per-lesson status for the active profile (if available).

    - If user or profile is missing, we just return all lessons as "new".
    """
    lesson_stats_map: Dict[str, Any] = {}

    try:
        session_user = request.session.get("user") if hasattr(request, "session") else None
        email = None
        if isinstance(session_user, dict):
            # adjust key if your login stores email differently
            email = session_user.get("email") or session_user.get("id")

        # If profile_id not provided explicitly, fall back to active_profile_id in session
        if profile_id is None and hasattr(request, "session"):
            profile_id = request.session.get("active_profile_id")

        if email and profile_id:
            store = FirestoreProfileStore()
            profile = store.get_profile(email=email, profile_id=profile_id)
            if profile:
                lesson_stats_map = profile.get("lesson_stats", {}) or {}
    except Exception:
        # Fail soft – frontend can still render "new" cards
        lesson_stats_map = {}

    lessons = curriculum.get_lessons(
        grade=grade,
        subject=subject,
        lesson_stats_map=lesson_stats_map,
    )
    return {"lessons": lessons}
