# backend/agents/tool_router.py
from __future__ import annotations

from typing import Dict, Any, Optional, List, Set, Union
from dataclasses import dataclass, field
import time, random

from backend.content import library
from backend.agents import logger_agent


class ToolError(Exception):
    pass


@dataclass
class Attempt:
    question_id: str
    choice_index: Optional[int] = None
    choice_text: Optional[str] = None
    correct: Optional[bool] = None
    at: float = field(default_factory=lambda: time.time())
    attempt_id: Optional[str] = None


@dataclass
class SessionState:
    current_question: Optional[Dict[str, Any]] = None  # ONLY mutated by get_question()
    question_seq: int = 0
    processed_attempt_ids: Set[str] = field(default_factory=set)
    last_answer: Optional[Attempt] = None
    progress_log: List[Attempt] = field(default_factory=list)


_SESSIONS: Dict[str, SessionState] = {}


def _get_session(session_id: str) -> SessionState:
    if not session_id or not session_id.strip():
        raise ToolError("session_id is required")
    sid = session_id.strip()
    sess = _SESSIONS.get(sid)
    if not sess:
        sess = SessionState()
        _SESSIONS[sid] = sess
    return sess


def get_current_question_for_session(session_id: str) -> Optional[Dict[str, Any]]:
    sess = _SESSIONS.get(session_id.strip())
    return sess.current_question if sess else None


class ToolRouter:
    """
    get_question: ONLY mutator of current_question (per session)
    update_answer: logs/evaluates; NEVER mutates current_question
    """

    # ---- helpers -------------------------------------------------------------
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
        choices = list(it.get("choices") or [])

        payload: Dict[str, Any] = {
            "question_id": it.get("id") or "",
            "type": it.get("type") or "",
            "question_text": question_text,
            "choices": choices,
            "answer": it.get("answer"),
            "topic": it.get("topic") or "",
            "difficulty": it.get("difficulty") or "",
            "prompt": question_text,          # deprecated, kept for safety

            # UI + orchestration
            "id": it.get("id") or "",
            "options": [str(c) for c in choices],
            "seq": seq,
        }
        if it.get("type") == "list" and it.get("list_spec"):
            payload["list_spec"] = it.get("list_spec")
        return payload

    # ---- public tools --------------------------------------------------------
    def get_question(
        self,
        *,
        grade: str,
        subject: str,
        topic: str,
        session_id: str,                    # MANDATORY
        last_question_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        sess = _get_session(session_id)

        all_items: List[Dict[str, Any]] = library.ITEMS or []
        if not all_items:
            raise ToolError("No items in library")

        items = self._filter_items(grade=grade, subject=subject, topic=topic)
        if not items:
            items = self._filter_items(grade=None, subject=None, topic=topic)
        if not items:
            items = all_items[:]
        if not items:
            raise ToolError("No matching items")

        if last_question_id:
            try:
                idx = next(i for i, it in enumerate(items) if it.get("id") == last_question_id)
                nxt = items[(idx + 1) % len(items)]
            except StopIteration:
                nxt = items[0]
        else:
            nxt = items[0]

        sess.question_seq += 1
        q_payload = self._to_question_payload(nxt, seq=sess.question_seq)
        sess.current_question = q_payload  # <-- ONLY place we mutate live question

        logger_agent.log_event({
            "type": "live_tool_get_question",
            "filters": {"grade": grade, "subject": subject, "topic": topic, "last_question_id": last_question_id},
            "chosen_id": nxt.get("id"),
            "session_id": session_id,
            "seq": sess.question_seq,
        })
        return q_payload

    def update_answer(
        self,
        *,
        question_id: str,
        user_answer: str,
        is_correct: bool,
        session_id: str,                    # MANDATORY
        attempt_id: Optional[str] = None,
        choice_index: Optional[int] = None,
    ) -> Dict[str, Any]:
        sess = _get_session(session_id)
        cq = sess.current_question

        # Idempotency
        if attempt_id and attempt_id in sess.processed_attempt_ids:
            return {"duplicate": True, "question_id": question_id, "session_id": session_id}
        if attempt_id:
            sess.processed_attempt_ids.add(attempt_id)

        if not cq or str(question_id) != str(cq.get("id")):
            logger_agent.log_event({
                "type": "live_tool_update_answer_stale",
                "question_id": question_id,
                "user_answer": user_answer,
                "is_correct": is_correct,
                "session_id": session_id,
                "current_question_id": cq.get("id") if cq else None,
            })
            return {"error": "stale_or_unknown_question", "current_question": cq}

        att = Attempt(
            question_id=str(question_id),
            choice_index=choice_index,
            choice_text=str(user_answer),
            correct=bool(is_correct),
            attempt_id=attempt_id or f"auto-{cq.get('seq')}",
        )
        sess.last_answer = att
        sess.progress_log.append(att)

        logger_agent.log_event({
            "type": "live_tool_update_answer",
            "question_id": question_id,
            "user_answer": user_answer,
            "is_correct": bool(is_correct),
            "session_id": session_id,
            "seq": cq.get("seq"),
        })

        # DO NOT mutate sess.current_question here.
        return {
            "ok": True,
            "correct": bool(is_correct),
            "feedback": "Great job!" if is_correct else "Not quite—try again.",
            "question": cq,
        }
