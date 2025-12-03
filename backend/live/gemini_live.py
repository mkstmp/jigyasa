# backend/live/gemini_live.py
import asyncio
import logging
from typing import Optional, Dict, Any, Callable, Awaitable

from google import genai
from google.genai import types

from backend.shared import config
from backend.agents.tool_router import ToolRouter

log = logging.getLogger("jigyasa")


class LiveBridgeError(Exception):
    pass


class GeminiLiveBridge:
    """
    Bridge: local WebSocket <-> Google Gemini Live (google-genai SDK)

    - start(): create an async live session (and start receiver task)
    - send_audio(bytes, mime): forward mic chunks
    - commit_segment(): optional end-of-turn hint
    - close(): tear down session

    session_cfg is owned by the app (not the model) and pins the QuestionSet:
      - grade / subject / topic
      - lesson_id (our QuestionSet id for this card)

    Tools for the model are deliberately minimal:

      get_question(last_question_id?)
        → fetch next question in the *current* QuestionSet.
        The model usually calls it with NO arguments.

      update_answer(question_id, user_answer, is_correct)
        → record what the child said and whether it was objectively correct.
    """

    def __init__(self):
        self._client: Optional[genai.Client] = None
        self._session = None               # live session (after __aenter__)
        self._session_cm = None            # async context manager returned by connect()
        self._recv_task: Optional[asyncio.Task] = None
        self.out_rate_hz = 24000           # Gemini Live AUDIO out rate

        # simple kid context (can be overridden by server)
        self.kid = {
            "name": "Atharv",
            "age": "5",
            "grade": "KG",
            "language": "English",
        }

        # session-level curriculum context (set from WS "hello" in main.py)
        # grade/subject/topic describe the track; lesson_id pins to a question set.
        self.session_cfg: Dict[str, Any] = {
            "grade": self.kid["grade"],
            "subject": "Math",
            "topic": "numbers",
            "lesson_id": None,  # QuestionSet id / lesson card id
        }

        # who owns this live session (set from server WS)
        self.parent_email: Optional[str] = None
        self.profile_id: Optional[str] = None

        # state / tools
        self.session_id: Optional[str] = None
        self._tool_router = ToolRouter()
        self._last_question_id: Optional[str] = None

        # UI / audio emitters bound by main.py
        self._ui_send: Optional[Callable[[Dict[str, Any]], Awaitable[None]]] = None
        self._audio_send: Optional[Callable[[bytes],       Awaitable[None]]] = None

        # logger alias (for convenience)
        self.log = log

    # ------------ bindings from server WS ------------
    def set_session(self, session_id: str):
        self.session_id = session_id

    def set_profile_context(
        self,
        parent_email: Optional[str],
        profile_id: Optional[str],
        kid: Optional[Dict[str, Any]] = None,
    ):
        """
        Attach parent/profile identity and optionally override kid persona.
        Called from the server when the WS is established.
        """
        self.parent_email = parent_email
        self.profile_id = profile_id
        if kid:
            # extend / override kid defaults
            self.kid.update({k: v for k, v in kid.items() if v is not None})
            # keep session_cfg grade in sync if provided
            if kid.get("grade"):
                self.session_cfg["grade"] = kid["grade"]

    def set_lesson_context(
        self,
        *,
        grade: Optional[str] = None,
        subject: Optional[str] = None,
        topic: Optional[str] = None,
        lesson_id: Optional[str] = None,
    ):
        """
        Called from main.py when the browser sends a 'hello' message.

        Example (in your WS handler):
            if msg["type"] == "hello":
                bridge.set_lesson_context(
                    grade=msg.get("grade"),
                    subject=msg.get("subject"),
                    topic=msg.get("topic"),
                    lesson_id=msg.get("lesson_id"),  # this is our QuestionSet id
                )

        The model never sees these fields; they are app-owned session state.
        """
        if grade:
            self.session_cfg["grade"] = grade
            self.kid["grade"] = grade  # keep prompt consistent
        if subject:
            self.session_cfg["subject"] = subject
        if topic:
            self.session_cfg["topic"] = topic
        if lesson_id:
            self.session_cfg["lesson_id"] = lesson_id

    def bind_ui_sender(self, fn: Callable[[Dict[str, Any]], Awaitable[None]]):
        """Main thread provides a coroutine to forward JSON to the browser WS."""
        self._ui_send = fn

    def bind_audio_sender(self, fn: Callable[[bytes], Awaitable[None]]):
        """Main thread provides a coroutine to forward PCM24k to the browser WS."""
        self._audio_send = fn

    async def _ui_emit(self, payload: Dict[str, Any]):
        if self._ui_send:
            await self._ui_send(payload)
        else:
            log.warning("UI sender not bound; dropping payload: %s", payload)

    # ------------ tool declarations (SDK objects) ------------
    def _build_tools(self):
        """
        Define the tools as far as Gemini is concerned.

        Note: get_question no longer exposes grade/subject/topic/lesson_id.
        Those are implicit in the session picked by the app.
        """

        get_question_decl = types.FunctionDeclaration(
            name="get_question",
            description=(
                "Fetch the next question in the CURRENT question set for this session. "
                "The app has already chosen the grade, subject, topic and lesson card. "
                "You usually call get_question() with NO arguments. "
                "Optionally, you may pass last_question_id to explicitly request the "
                "next question after a specific one."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "last_question_id": {
                        "type": "string",
                        "description": (
                            "The id of the question you most recently asked. "
                            "If provided, the NEXT question after this id will be returned."
                        ),
                    },
                },
                "required": [],
            },
        )

        update_answer_decl = types.FunctionDeclaration(
            name="update_answer",
            description=(
                "Record the child's answer and whether it was actually correct. "
                "You must call this exactly once for each question you ask. "
                "The is_correct field MUST be true only if the child's answer is objectively correct."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "question_id": {"type": "string"},
                    "user_answer": {"type": "string"},
                    "is_correct":  {"type": "boolean"},
                },
                "required": ["question_id", "user_answer", "is_correct"],
            },
        )

        return [types.Tool(function_declarations=[get_question_decl, update_answer_decl])]

    # ------------ live lifecycle ------------
    async def start(self):
        if not config.GOOGLE_API_KEY:
            raise LiveBridgeError("GOOGLE_API_KEY missing")

        log.info(
            "GeminiLiveBridge.start: init client (model=%s)",
            config.GEMINI_LIVE_MODEL
        )
        try:
            self._client = genai.Client(api_key=config.GOOGLE_API_KEY)
        except Exception as e:
            raise LiveBridgeError(f"Failed to init google-genai client: {e}")

        # System instruction as Content
        sys_text = (
            f"You are conducting a test(with fixed questions) for {self.kid['name']}, "
            f"a {self.kid['age']}-year-old child in grade {self.kid['grade']}. "
            f"{self.kid['name']} speak in {self.kid['language']}.\n\n"

            "TOOL WORKFLOW:\n"
            "1) To start or move to the next question, call get_question() with NO arguments.\n"
            "   (If you really need to be explicit, you may pass last_question_id with the id of the\n"
            "    question you just asked, but this is usually unnecessary.)\n"
            "2) Read the returned question_text VERBATIM to the child.\n"
            "3) Listen to the child's spoken answer.\n"
            "4) Call update_answer(question_id, user_answer, is_correct) EXACTLY ONCE for that question:\n"
            "     - question_id MUST be the id of the last question you got from get_question().\n"
            "     - is_correct = true ONLY if the answer is objectively correct.\n"
            "       If the answer is wrong, incomplete, off-topic, 'I don't know', or silence,\n"
            "       you MUST set is_correct = false.\n"
            "5) Give very short spoken feedback (praise or gentle 'that's not right'),\n"
            "   and then immediately call get_question() for the next question.\n\n"

            "AUDIO STYLE:\n"
            "   - Use simple language and speak clearly.\n"
            "Session rules:\n"
            "  - For each question, the child gets ONE  attempt.\n"
            "  - When you understand the child’s answer, you MUST call the tool `update_answer` exactly once for that question."
            "  - After you get the `update_answer` result:\n "
            "       • If `correct` is true: briefly praise the child, then immediately call `get_question` for the next question (unless `lesson_done` is true).\n"
            "       • If `correct` is false: briefly say the correct answer and encourage the child, then immediately call `get_question` for the next question (unless `lesson_done` is true).\n"
            "  - Do NOT ask the child to “try again” on the same question.\n"
            "  - Do NOT repeat the same question or wait for another answer before calling `get_question`.\n"
            "  - Never call `update_answer` twice for the same question.\n"


            "Note: Your are not a tutor, you are conducting test! Move to next question even if kid's response is wrong!"
        )

        system_instruction = types.Content(
            role="system",
            parts=[types.Part(text=sys_text)],
        )
        log.info("System Instruction: %s", sys_text)

        try:
            live_config = types.LiveConnectConfig(
                response_modalities=["AUDIO"],
                system_instruction=system_instruction,
                tools=self._build_tools(),
            )
        except Exception as e:
            raise LiveBridgeError(f"Failed to build LiveConnectConfig: {e}")

        try:
            self._session_cm = self._client.aio.live.connect(
                model=f"models/{config.GEMINI_LIVE_MODEL}",
                config=live_config,
            )
            self._session = await self._session_cm.__aenter__()
        except Exception as e:
            raise LiveBridgeError(f"Failed to connect to Gemini Live: {e}")

        log.info("GeminiLiveBridge.start: live session established")

        # Start background receiver (model will speak after first audio / end_of_turn)
        self._recv_task = asyncio.create_task(self._pump_from_google())

    # ------------ tool backends (delegate to ToolRouter) ------------
    async def _tool_get_question(self, args: dict):
        """
        Backend for the get_question tool.

        The model is only allowed to supply last_question_id.
        All curriculum routing (grade/subject/topic/lesson_id) is owned
        by session_cfg, which was populated from the UI when the session started.
        """
        if not self.session_id:
            self.log.error("tool_get_question with no session_id set")
            payload = {
                "type": "tool_result",
                "name": "get_question",
                "error": "missing_session_id",
            }
            await self._ui_emit(payload)
            return {"error": "missing_session_id"}

        cfg = self.session_cfg or {}
        grade = str(cfg.get("grade") or self.kid["grade"])
        subject = str(cfg.get("subject") or "Math")
        topic = str(cfg.get("topic") or "numbers")
        lesson_id = cfg.get("lesson_id")

        last_qid = args.get("last_question_id") or self._last_question_id

        self.log.info(
            "tool_get_question context: grade=%s subject=%s topic=%s "
            "lesson_id=%s last_question_id=%s",
            grade,
            subject,
            topic,
            lesson_id,
            last_qid,
        )

        question = self._tool_router.get_question(
            grade=grade,
            subject=subject,
            topic=topic,
            lesson_id=lesson_id,
            session_id=self.session_id,
            last_question_id=last_qid,
            profile_id=self.profile_id,
            user_email=self.parent_email,
        )
        self._last_question_id = question.get("id")

        # Mirror to UI
        await self._ui_emit(
            {"type": "tool_result", "name": "get_question", "output": question}
        )
        return question

    async def _tool_update_answer(self, args: dict):
        if not self.session_id:
            self.log.error("tool_update_answer with no session_id set")
            payload = {
                "type": "tool_result",
                "name": "update_answer",
                "error": "missing_session_id",
            }
            await self._ui_emit(payload)
            return {"error": "missing_session_id"}

        result = self._tool_router.update_answer(
            question_id=args["question_id"],
            user_answer=str(args.get("user_answer") or args.get("choice") or ""),
            is_correct=bool(args.get("is_correct", False)),
            session_id=self.session_id,
            attempt_id=args.get("attempt_id"),
            choice_index=args.get("choice_index"),
        )
        await self._ui_emit(
            {"type": "tool_result", "name": "update_answer", "output": result}
        )
        return result

    # ------------ send tool result back to model ------------
    async def _send_tool_result(
        self, *, name: str, call_id: Optional[str], result_obj: Dict[str, Any]
    ):
        """
        Send a tool result back to the model across SDK variants.

        Prefer (new):  session.send_tool_response(function_responses=[FunctionResponse(...)])
        Fallbacks:     session.send_realtime_input(function_responses=[...])
                       session.send(function_responses=[...])
        """
        if not call_id:
            log.warning("Tool call (name=%s) had no call_id; cannot send result.", name)
            return

        fr = types.FunctionResponse(name=name, id=call_id, response=result_obj)

        # ---- Path A: new API (if present)
        try:
            send_tool_resp = getattr(self._session, "send_tool_response", None)
            if callable(send_tool_resp):
                await send_tool_resp(function_responses=[fr])  # keyword-only
                log.info(
                    "Sent tool result via send_tool_response (call_id=%s)", call_id
                )
                return
        except Exception as e:
            log.debug("send_tool_response path failed: %s", e)

        # ---- Path B: realtime input path (many builds support this)
        try:
            send_rt = getattr(self._session, "send_realtime_input", None)
            if callable(send_rt):
                await send_rt(function_responses=[fr])  # no 'input' required
                log.info(
                    "Sent tool result via send_realtime_input(function_responses=...) "
                    "(call_id=%s)",
                    call_id,
                )
                return
        except Exception as e:
            log.debug("send_realtime_input(function_responses=...) failed: %s", e)

        # ---- Path C: plain send() with function_responses kwarg (some builds)
        try:
            await self._session.send(function_responses=[fr])  # no 'input' kw required
            log.info(
                "Sent tool result via send(function_responses=[...]) (call_id=%s)",
                call_id,
            )
            return
        except Exception as e:
            log.exception(
                "All tool-response paths failed (call_id=%s): %s", call_id, e
            )

    # ------------ live receive loop ------------
    async def _pump_from_google(self):
        log.info("GeminiLiveBridge._pump_from_google: receiver started")
        try:
            while True:
                turn = self._session.receive()
                async for response in turn:
                    # 1) Audio frames (PCM16 @ 24kHz, mono)
                    if getattr(response, "data", None):
                        pcm_bytes = response.data
                        if self._audio_send:
                            await self._audio_send(pcm_bytes)

                    # 2) Tool calls
                    tool_call = getattr(response, "tool_call", None)
                    if tool_call is not None:
                        log.info(
                            "RAW tool_call: %s",
                            getattr(tool_call, "function_calls", None),
                        )
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

    async def _handle_tool_call(self, tool_call):
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

            if result is None:
                result = {"ok": True}

            try:
                await self._send_tool_result(
                    name=name or "unknown", call_id=call_id, result_obj=result
                )
            except Exception as e:
                log.exception("send tool_result failed: %s", e)

    # ------------ public streaming API for server WS ------------
    async def send_audio(self, data: bytes, mime: Optional[str] = None):
        """
        Forward mic chunk to Gemini.
        Browser is sending PCM16 @ 16kHz (or 24k if you adjusted FE):
          mime ~ 'audio/pcm;rate=16000' (or 'audio/pcm;rate=24000')
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
            await self._session.send(input="", end_of_turn=True)  # legacy builds
        except Exception:
            pass

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
            log.info("GeminiLiveBridge.close: done")

    async def send_text(self, text: str):
        """Send a text message to the model (e.g., to kickstart conversation)."""
        if not self._session:
            self.log.warning("Cannot send_text: session not active")
            return
        try:
            # end_of_turn=True forces the model to generate a response
            await self._session.send(input=text, end_of_turn=True)
        except Exception as e:
            self.log.error("send_text failed: %s", e)
