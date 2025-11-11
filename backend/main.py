# backend/main.py
from __future__ import annotations

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.websockets import WebSocketState
from typing import Dict, Any
import os, json, asyncio, contextlib, logging

from backend.shared.schemas import EventRecord, ProfileState
from backend.shared import config
from backend.agents import logger_agent
from backend.live.gemini_live import GeminiLiveBridge, LiveBridgeError
from backend.agents.tool_router import get_current_question_for_session

# -----------------------------------------------------------------------------
# App setup
# -----------------------------------------------------------------------------
app = FastAPI(title="Jigyāsa API (v4 - LIVE only)", version="0.4.0")

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

# Static files (images, etc.)
STATIC_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "static"))
os.makedirs(STATIC_DIR, exist_ok=True)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# -----------------------------------------------------------------------------
# Minimal in-memory profile state (optional, for UI counters)
# -----------------------------------------------------------------------------
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
# Read-only sync: must supply session_id (profile_id)
# -----------------------------------------------------------------------------
@app.get("/live/current_question")
def http_get_current_question(session_id: str = Query(..., description="ProfileId acts as SessionId")):
    q = get_current_question_for_session(session_id)
    if not q:
        raise HTTPException(status_code=404, detail="no current question")
    q = {k: v for k, v in q.items() if k != "_answer"}
    return {"question": q}

# -----------------------------------------------------------------------------
# Gemini LIVE WebSocket bridge (PCM path)
# -----------------------------------------------------------------------------
@app.websocket("/live/ws")
async def live_ws(ws: WebSocket):
    await ws.accept()
    sid = (ws.query_params.get("session_id") or "kid_demo_001").strip()
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
    # BIND PUSH SENDERS (JSON + AUDIO) — this is the only downstream path
    bridge.bind_ui_sender(safe_send_json)
    bridge.bind_audio_sender(safe_send_bytes)

    try:
        bridge.set_session(sid)   # set first to avoid races
        await bridge.start()
        await safe_send_json({"type": "session", "session_id": sid})
        await safe_send_json({"type": "ready"})
        await safe_send_json({"type": "pcm_meta", "rate": bridge.out_rate_hz})

        # Browser mic/control loop (NO downstream pump here)
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
                    # Optional pass-through (if you allow FE to initiate tools)
                    # The live model should orchestrate; feel free to ignore.
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
