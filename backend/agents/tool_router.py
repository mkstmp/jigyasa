# backend/agents/tool_router.py
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Any, Optional, List, Set
import time

from backend.content import library
from backend.agents import logger_agent


class ToolError(Exception):
    pass


# -------------------------------
# In-memory session store (demo)
# -------------------------------
@dataclass
class Attempt:
    question_id: str
    choice_index: Optional[int] = None
    choice_text: Optional[str] = None
    correct: bool = False
    attempt_id: str = ""


@dataclass
class SessionState:
    session_id: str
    current_question: Optional[Dict[str, Any]] = None
    last_answer: Optional[Attempt] = None
    progress_log: List[Attempt] = field(default_factory=list)
    processed_attempt_ids: Set[str] = field(default_factory=set)
    seq: int = 0  # increments on each get_question


_SESSIONS: Dict[str, SessionState] = {}


def _get_session(session_id: str) -> SessionState:
    if not session_id:
        raise ToolError("session_id required")
    st = _SESSIONS.get(session_id)
    if not st:
        st = SessionState(session_id=session_id)
        _SESSIONS[session_id] = st
    return st


def get_current_question_for_session(session_id: str) -> Optional[Dict[str, Any]]:
    """Used by /live/current_question for the UI to poll; safe copy w/o answer."""
    st = _SESSIONS.get(session_id)
    if not st or not st.current_question:
        return None
    q = dict(st.current_question)
    q.pop("_answer", None)
    return q


# -------------------------------
# Tool Router
# -------------------------------
class ToolRouter:
    """
    get_question: chooses next item from library (filtered) and updates the session's current_question.
    update_answer: stateless; records/journals the answer. NO dependence on current_question.
    """

    # ---- helpers ----
    def _norm(self, s: Optional[str]) -> str:
        return (s or "").strip().lower()

    def _filter_items(
        self,
        *,
        grade: Optional[str],
        subject: Optional[str],
        topic: Optional[str],
    ) -> List[Dict[str, Any]]:
        items = library.ITEMS or []
        tg = self._norm(grade)
        sj = self._norm(subject)
        tp = self._norm(topic)

        if tp:
            items = [it for it in items if self._norm(it.get("topic")) == tp]
        if sj:
            items = [it for it in items if self._norm(it.get("subject")) in ("", sj, None)]
        if tg:
            items = [it for it in items if self._norm(it.get("grade")) in ("", tg, None)]
        return items

    def _to_question_payload(self, it: Dict[str, Any], *, seq: int) -> Dict[str, Any]:
        question_text = (
            it.get("question_text")
            or it.get("prompt_tts")
            or it.get("prompt_visual")
            or ""
        )

        payload: Dict[str, Any] = {
            "id": it.get("id") or "",
            "question_id": it.get("id") or "",
            "type": it.get("type") or "open",
            "question_text": question_text,
            "choices": list(it.get("choices") or []),
            "options": list(it.get("choices") or []),  # FE compatibility
            "answer": it.get("answer"),
            "topic": it.get("topic") or "",
            "difficulty": it.get("difficulty") or "",
            "grade": it.get("grade") or "",
            "subject": it.get("subject") or "",
            "prompt": question_text,  # transitional; can remove later
            "seq": seq,
        }

        if it.get("type") == "list" and it.get("list_spec"):
            payload["list_spec"] = it.get("list_spec")
        return payload

    # ---- tools ----
    def get_question(
        self,
        *,
        grade: str,
        subject: str,
        topic: str,
        session_id: str,                      # mandatory
        last_question_id: Optional[str] = None
    ) -> Dict[str, Any]:
        all_items: List[Dict[str, Any]] = library.ITEMS or []
        if not all_items:
            raise ToolError("No items in library")

        # strict, then relax, then fallback
        items = self._filter_items(grade=grade, subject=subject, topic=topic)
        if not items:
            items = self._filter_items(grade=None, subject=None, topic=topic)
        if not items:
            items = all_items[:]
        if not items:
            raise ToolError("No matching items")

        # pick next after last_question_id (within the filtered list)
        if last_question_id:
            try:
                idx = next(i for i, it in enumerate(items) if it.get("id") == last_question_id)
                nxt = items[(idx + 1) % len(items)]
            except StopIteration:
                nxt = items[0]
        else:
            nxt = items[0]

        sess = _get_session(session_id)
        sess.seq += 1

        # decorate and store as current question
        q_payload = self._to_question_payload(nxt, seq=sess.seq)
        # keep canonical answer only server-side (omit from /live/current_question)
        q_payload["_answer"] = nxt.get("answer")
        sess.current_question = q_payload

        logger_agent.log_event({
            "type": "live_tool_get_question",
            "filters": {"grade": grade, "subject": subject, "topic": topic, "last_question_id": last_question_id},
            "chosen_id": nxt.get("id"),
            "session_id": session_id,
            "seq": sess.seq,
        })
        return q_payload

    def update_answer(
        self,
        *,
        question_id: str,
        user_answer: str,
        is_correct: bool,
        session_id: str,                    # mandatory
        attempt_id: Optional[str] = None,
        choice_index: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Stateless record of an answer. DO NOT depend on sess.current_question.
        Only enforce idempotency via attempt_id.
        """
        sess = _get_session(session_id)

        # Idempotency
        if attempt_id and attempt_id in sess.processed_attempt_ids:
            return {"duplicate": True, "question_id": question_id, "session_id": session_id}
        if attempt_id:
            sess.processed_attempt_ids.add(attempt_id)

        # Journal attempt
        att = Attempt(
            question_id=str(question_id),
            choice_index=choice_index,
            choice_text=str(user_answer),
            correct=bool(is_correct),
            attempt_id=attempt_id or f"ts-{int(time.time())}",
        )
        sess.last_answer = att
        sess.progress_log.append(att)

        # Observability
        logger_agent.log_event({
            "type": "live_tool_update_answer",
            "question_id": question_id,
            "user_answer": user_answer,
            "is_correct": bool(is_correct),
            "session_id": session_id,
        })

        # Do NOT mutate sess.current_question here.
        # Do NOT return current_question here.
        return {
            "ok": True,
            "correct": bool(is_correct),
            "feedback": "Great job!" if is_correct else "Not quite—try again.",
        }
