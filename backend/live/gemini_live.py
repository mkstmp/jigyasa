# backend/live/gemini_live.py
import asyncio
import logging
from typing import AsyncGenerator, Optional, Dict, Any

from google import genai
from google.genai import types

from backend.shared import config
from backend.agents.tool_router import ToolRouter, ToolError

log = logging.getLogger("jigyasa")


class LiveBridgeError(Exception):
    pass


class GeminiLiveBridge:
    """
    Bridge: local WebSocket <-> Google Gemini Live (google-genai SDK)

    - start(): create an async live session
    - send_audio(bytes, mime): forward mic chunks
    - commit_segment(): optional end-of-turn hint
    - receive_audio(): async generator of PCM 24kHz 16-bit mono chunks (response.data)
    - close(): tear down session
    """

    def __init__(self):
        self._client: Optional[genai.Client] = None
        self._session = None                     # live session (after __aenter__)
        self._session_cm = None                  # async context manager returned by connect()
        self._recv_task: Optional[asyncio.Task] = None
        self._audio_queue: asyncio.Queue[bytes] = asyncio.Queue()
        self._closed = asyncio.Event()
        self.out_rate_hz = 24000  # Gemini Live AUDIO out rate

        # simple kid context
        self.kid = {
            "name": "Atharv",
            "age": "5",
            "grade": "KG",
            "language": "en-IN",   # Indian English
        }

        # tool routing
        self._tool_router = ToolRouter()
        self._last_question_id: Optional[str] = None

    # ---- tool declarations (SDK objects) ----
    def _build_tools(self):
        get_question_decl = types.FunctionDeclaration(
            name="get_question",
            description=(
                "Fetch the next question for the child given grade, subject, and topic. "
                "If last_question_id is omitted, fetch the first question; otherwise fetch the one after it."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "grade":  {"type": "string"},
                    "subject":{"type": "string"},
                    "topic":  {"type": "string"},
                    "last_question_id": {"type": "string"}
                },
                "required": ["grade", "subject", "topic"]
            },
        )

        update_answer_decl = types.FunctionDeclaration(
            name="update_answer",
            description="Record the child's answer and whether it was correct.",
            parameters={
                "type": "object",
                "properties": {
                    "question_id": {"type": "string"},
                    "user_answer": {"type": "string"},
                    "is_correct":  {"type": "boolean"}
                },
                "required": ["question_id", "user_answer", "is_correct"]
            },
        )

        return [types.Tool(function_declarations=[get_question_decl, update_answer_decl])]

    async def start(self):
        if not config.GOOGLE_API_KEY:
            raise LiveBridgeError("GOOGLE_API_KEY missing")

        log.info("GeminiLiveBridge.start: init client (model=%s)", config.GEMINI_LIVE_MODEL)
        try:
            self._client = genai.Client(
                api_key=config.GOOGLE_API_KEY,
            )
        except Exception as e:
            raise LiveBridgeError(f"Failed to init google-genai client: {e}")

        # System instruction as Content (type-correct for Live config)
        sys_text = (
            f"You are a friendly, patient tutor speaking with a young child named {self.kid['name']} "
            f"(age {self.kid['age']}, grade {self.kid['grade']}). "
            f"Speak in {self.kid['language']} (Indian English). "
            "ALWAYS use the available tools to fetch questions and record answers. "
            "Do NOT invent questions yourself—call get_question first to obtain one. "
            "Greet the child briefly, then call get_question with grade='KG', subject='Math', topic='Numbers'. "
            "Ask one question at a time, listen, call update_answer with your judgement, and then move to the next. "
            "Keep responses short, warm, and age-appropriate. Use audio only."
        )
        system_instruction = types.Content(role="system", parts=[types.Part(text=sys_text)])

        try:
            live_config = types.LiveConnectConfig(
                response_modalities=["AUDIO"],          # keep minimal to avoid 1007 errors
                system_instruction=system_instruction,  # correct type
                tools=self._build_tools(),              # Tool declarations
            )
        except Exception as e:
            raise LiveBridgeError(f"Failed to build LiveConnectConfig: {e}")

        try:
            self._session_cm = self._client.aio.live.connect(
                model=f"models/{config.GEMINI_LIVE_MODEL}",
                config=live_config,
            )
            # Span the WS lifetime
            self._session = await self._session_cm.__aenter__()
        except Exception as e:
            raise LiveBridgeError(f"Failed to connect to Gemini Live: {e}")

        log.info("GeminiLiveBridge.start: live session established")

        # Prime with a text nudge the live SDK accepts
        # Get values from context
        grade = self.kid['grade']
        subject = "Math"  # Or add 'subject' to self.kid
        topic = "Numbers" # Or add 'topic' to self.kid

        try:
            await self._session.send(
                input=(
                    f"Please greet the child briefly in Indian English and then call the tool "
                    f"`get_question` with {{grade:'{grade}', subject:'{subject}', topic:'{topic}'}}. "
                    f"Do not create your own question; use the tool result."
                )
            )
            log.info("Primer sent: greet + get_question(%s/%s/%s)", grade, subject, topic)
        except Exception as e:
            log.warning("Primer send failed: %s", e)

        # Start background receiver
        self._recv_task = asyncio.create_task(self._pump_from_google())

    # ---- tool backends (delegate to ToolRouter) ----
    async def _tool_get_question(self, args: Dict[str, Any]) -> Dict[str, Any]:
        try:
            question = self._tool_router.get_question(
                grade=str(args.get("grade") or self.kid["grade"]),
                subject=str(args.get("subject") or "Math"),
                topic=str(args.get("topic") or "Numbers"),
                last_question_id=args.get("last_question_id") or self._last_question_id
            )
            self._last_question_id = question.get("question_id")
            return question
        except ToolError as e:
            log.warning("ToolRouter get_question failed: %s", e)
            return {"error": str(e)}

    async def _tool_update_answer(self, args: Dict[str, Any]) -> Dict[str, Any]:
        try:
            return self._tool_router.update_answer(
                question_id=str(args.get("question_id") or ""),
                user_answer=str(args.get("user_answer") or ""),
                is_correct=bool(args.get("is_correct")),
            )
        except ToolError as e:
            log.warning("ToolRouter update_answer failed: %s", e)
            return {"error": str(e)}

    # ---- adaptive tool-result sender (works across SDK variants) ----

    async def _send_tool_result(self, *, name: str, call_id: Optional[str], result_obj: Dict[str, Any]):
        """
        Send a tool result back to the model using the correct method
        from the stable Live API.
        """
        if not call_id:
            log.warning("Tool call (name=%s) had no call_id; cannot send result.", name)
            return

        try:
            # 1. Create the FunctionResponse object
            fr = types.FunctionResponse(
                name=name,
                id=call_id,
                response=result_obj
            )

            # 2. Send it using session.send_tool_response()
            # This is the correct method in the new API
            await self._session.send_tool_response(
                function_responses=[fr]  # Pass it as a list
            )
            log.info("Sent tool result via send_tool_response (call_id=%s)", call_id)

        except Exception as e:
            log.exception("send_tool_response failed (call_id=%s): %s", call_id, e)

    

    # ---- live receive loop ----
    async def _pump_from_google(self):
        log.info("GeminiLiveBridge._pump_from_google: receiver started")
        try:
            while True:
                turn = self._session.receive()
                async for response in turn:
                    # 1) audio
                    if response.data:
                        self._audio_queue.put_nowait(response.data)

                    tool_call = getattr(response, "tool_call", None)
                    if tool_call is not None:
                        log.info("RAW tool_call: %s", getattr(tool_call, "function_calls", None))
                        try:
                            await self._handle_tool_call(tool_call)
                        except Exception as e:
                            log.exception("tool_call handler failed: %s", e)

        except asyncio.CancelledError:
            pass
        except Exception as e:
            log.warning("GeminiLiveBridge._pump_from_google: exception: %s", e)
        finally:
            log.info("GeminiLiveBridge._pump_from_google: receiver finished")
            self._closed.set()

    async def _handle_tool_call(self, tool_call):
        """
        Parse FunctionCall(s) and dispatch to our tool router, then send the result
        back using whatever tool-response path the SDK supports.
        """
        calls = getattr(tool_call, "function_calls", None) or []
        for fc in calls:
            name = getattr(fc, "name", None)
            args = getattr(fc, "args", {}) or {}
            call_id = getattr(fc, "id", None)
            log.info("LIVE tool_call parsed: name=%s args=%s id=%s", name, args, call_id)

            result: Dict[str, Any] = {"error": f"Unknown tool: {name}"}
            if name == "get_question":
                result = await self._tool_get_question(args)
            elif name == "update_answer":
                result = await self._tool_update_answer(args)

            # Send result back
            try:
                await self._send_tool_result(name=name or "unknown", call_id=call_id, result_obj=result)
            except Exception as e:
                log.exception("send tool_result failed: %s", e)

    # ---- public streaming API for server WS ----
    async def send_audio(self, data: bytes, mime: Optional[str] = None):
        """
        Forward mic chunk to Gemini. Browser is sending PCM16 @ 16kHz:
          mime ~ 'audio/pcm;rate=16000'
        """
        if not self._session:
            return
        mime_type = mime or "audio/pcm;rate=16000"
        try:
            await self._session.send(input={"data": data, "mime_type": mime_type})
        except Exception as e:
            log.debug("GeminiLiveBridge.send_audio: send failed: %s", e)

    async def commit_segment(self):
        """Optional end-of-turn hint."""
        if not self._session:
            return
        try:
            await self._session.send(end_of_turn=True)
        except Exception:
            pass

    async def receive_audio(self) -> AsyncGenerator[bytes, None]:
        """Yield PCM audio to caller (server WS), which relays to the browser."""
        while True:
            if self._closed.is_set() and self._audio_queue.empty():
                break
            try:
                chunk = await asyncio.wait_for(self._audio_queue.get(), timeout=0.5)
                yield chunk
            except asyncio.TimeoutError:
                if self._closed.is_set():
                    break
                continue

    async def close(self):
        log.info("GeminiLiveBridge.close: closing session")
        try:
            if self._recv_task:
                self._recv_task.cancel()
                try:
                    await self._recv_task
                except Exception:
                    pass
            if self._session_cm is not None:
                await self._session_cm.__aexit__(None, None, None)
        finally:
            self._closed.set()
            log.info("GeminiLiveBridge.close: done")
