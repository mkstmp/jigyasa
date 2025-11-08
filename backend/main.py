# backend/main.py
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.websockets import WebSocketState
from typing import Dict, Any
import os, json, asyncio, contextlib, logging

from backend.shared.schemas import EventRecord, ProfileState
from backend.shared import config
from backend.live.gemini_live import GeminiLiveBridge, LiveBridgeError

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
# Gemini LIVE WebSocket bridge (PCM path)
# -----------------------------------------------------------------------------
@app.websocket("/live/ws")
async def live_ws(ws: WebSocket):
    """
    WS protocol (browser <-> server):
      - Client sends {"type":"hello","mime":"audio/pcm;rate=16000"} once connected
      - Then streams binary mic frames (PCM16 @16k)
      - Server forwards to Gemini Live; streams PCM24k back (binary)
      - {"type":"commit"} marks end-of-turn, {"type":"done"} closes

    NOTE: No /curriculum/next. The model/tooling now drives question flow end-to-end.
    """
    await ws.accept()
    peer = f"{ws.client.host}:{ws.client.port if ws.client else 'unknown'}"
    log.info("WS accepted from %s", peer)

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
    downstream_task = None

    try:
        # Start Gemini live
        try:
            await bridge.start()
            await safe_send_json({"type": "ready"})
            await safe_send_json({"type": "pcm_meta", "rate": bridge.out_rate_hz})
            log.info("Gemini Live bridge started for %s", peer)
        except LiveBridgeError as e:
            await safe_send_json({"type": "error", "error": str(e)})
            with contextlib.suppress(Exception):
                await ws.close()
            log.error("Bridge init failed (%s): %s", peer, e)
            return
        except Exception as e:
            await safe_send_json({"type": "error", "error": "INIT_FAILED"})
            with contextlib.suppress(Exception):
                await ws.close()
            log.exception("Bridge unexpected init error (%s): %s", peer, e)
            return

        # Model audio -> browser (PCM24k)
        async def pump_downstream():
            try:
                async for audio_chunk in bridge.receive_audio():
                    await safe_send_bytes(audio_chunk)
                    log.debug("WS %s <- %d bytes (pcm24k)", peer, len(audio_chunk))
            except Exception:
                # Quietly exit on disconnect
                pass

        downstream_task = asyncio.create_task(pump_downstream())

        # Browser mic + control
        mime = "audio/pcm;rate=16000"
        while True:
            msg = await ws.receive()

            # client closed
            if msg.get("type") == "websocket.disconnect":
                log.info("WS disconnected from %s", peer)
                break

            # control (JSON)
            if isinstance(msg.get("text"), str):
                txt = msg["text"]
                log.debug("WS %s text: %s", peer, txt)
                if txt.startswith("{"):
                    with contextlib.suppress(Exception):
                        obj = json.loads(txt)
                        t = obj.get("type")
                        if t == "hello":
                            if obj.get("mime"):
                                mime = obj["mime"]
                            await safe_send_json({"type": "ack", "mime": mime})
                            log.info("WS %s hello ack (mime=%s)", peer, mime)
                        elif t == "commit":
                            with contextlib.suppress(Exception):
                                await bridge.commit_segment()
                            log.info("WS %s commit sent to LIVE", peer)
                        elif t == "done":
                            log.info("WS %s done received", peer)
                            break
                        # Optional: ignore/allow other control types (e.g., "say"/"prime") if your FE sends them
                continue

            # binary = mic audio (PCM16 @16k)
            data = msg.get("bytes")
            if data:
                with contextlib.suppress(Exception):
                    await bridge.send_audio(data, mime)
                    log.debug("WS %s -> %d bytes (pcm16)", peer, len(data))

    except WebSocketDisconnect:
        log.info("WS %s disconnected", peer)
    except Exception as e:
        log.exception("WS %s error: %s", peer, e)
    finally:
        # Close Gemini bridge
        with contextlib.suppress(Exception):
            await bridge.close()

        # Cancel downstream pump quietly
        if downstream_task:
            downstream_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.shield(downstream_task)

        with contextlib.suppress(Exception):
            await ws.close()

        log.info("WS %s closed", peer)
