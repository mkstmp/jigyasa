# backend/agents/tool_router.py
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Any, Optional, List, Set
import time
import logging

from backend.content import library
from backend.agents import logger_agent
from backend.shared.profile_store import FirestoreProfileStore


class ToolError(Exception):
    pass


logger = logging.getLogger("jigyasa.tool_router")
profile_store = FirestoreProfileStore()


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

    # who this live session belongs to (set by get_question via caller context)
    user_email: Optional[str] = None
    profile_id: Optional[str] = None

    # lesson tracking (per live session / QuestionSet)
    active_lesson_id: Optional[str] = None
    answered_ids_for_lesson: Set[str] = field(default_factory=set)


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


def reset_session_state(session_id: str) -> None:
    """
    Clear in-memory state for a given session_id.

    Called when a new Live WebSocket session starts, so per-run stats
    (progress_log, seq, etc.) are fresh and lesson_done detection works
    per run, not across many days.
    """
    if not session_id:
        return
    _SESSIONS.pop(session_id, None)


# -------------------------------
# Tool Router
# -------------------------------
class ToolRouter:
    """
    get_question:
      - chooses the next item in the current QuestionSet (lesson_id) and
        updates the session's current_question.
      - If lesson_id is not provided, it falls back to the session's
        active_lesson_id, then to grade/subject/topic, then to the whole library.

    update_answer:
      - stateless; records/journals the answer.
      - NO dependence on current_question (only uses IDs for safety/logging).
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
            items = [
                it
                for it in items
                if self._norm(it.get("subject")) in ("", sj, None)
            ]
        if tg:
            items = [
                it
                for it in items
                if self._norm(it.get("grade")) in ("", tg, None)
            ]
        return items

    def _to_question_payload(
        self, it: Dict[str, Any], *, seq: int, **extra: Any
    ) -> Dict[str, Any]:
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

        # attach lesson metadata if present
        if it.get("lesson_id"):
            payload["lesson_id"] = it.get("lesson_id")

        # any extra metadata (index_in_lesson, total_in_lesson, has_more, etc.)
        payload.update(extra)

        # list-type extra spec
        if it.get("type") == "list" and it.get("list_spec"):
            payload["list_spec"] = it.get("list_spec")

        # NEW: forward media to the UI (for shapes, pictures, etc.)
        if it.get("media"):
            # it["media"] is already a plain dict from JSON / ContentItem
            payload["media"] = it.get("media")

        # Optional: forward tags if present (can be useful later)
        if it.get("tags"):
            payload["tags"] = list(it.get("tags") or [])

        return payload


    # ---- tools ----
    def get_question(
        self,
        *,
        session_id: str,                    # mandatory
        last_question_id: Optional[str] = None,
        profile_id: Optional[str] = None,
        user_email: Optional[str] = None,
        lesson_id: Optional[str] = None,    # QuestionSet / lesson card id
        grade: Optional[str] = None,
        subject: Optional[str] = None,
        topic: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Choose the next question for this live session.

        Primary routing:
          - If we know a lesson_id (QuestionSet id) either from the call or from
            the per-session state, we stick to that set.
          - If that yields no items, we fall back to topic-based filtering
            (grade+subject+topic), then to the whole library.

        last_question_id (if provided) is used to advance within the filtered
        list; otherwise we start at the first item in that list.
        """
        all_items: List[Dict[str, Any]] = library.ITEMS or []
        if not all_items:
            raise ToolError("No items in library")

        sess = _get_session(session_id)

        # Attach identity context once per session
        if profile_id and not sess.profile_id:
            sess.profile_id = profile_id
        if user_email and not sess.user_email:
            sess.user_email = user_email

        # Normalized optional filters (used mainly as fallbacks + logging)
        g = grade or ""
        s = subject or ""
        t = topic or ""

        # ---- strict by lesson_id (QuestionSet) when we have one ----
        effective_lesson_id = lesson_id or sess.active_lesson_id
        items: List[Dict[str, Any]] = []

        if effective_lesson_id:
            items = [
                it
                for it in all_items
                if str(it.get("lesson_id")) == str(effective_lesson_id)
            ]
            if not items:
                logger.warning(
                    "No items for lesson_id=%s; falling back to topic-based filtering "
                    "(grade=%s, subject=%s, topic=%s)",
                    effective_lesson_id,
                    g,
                    s,
                    t,
                )
                effective_lesson_id = None

        # ---- topic-based fallback (if no valid lesson_id) ----
        if not effective_lesson_id:
            items = self._filter_items(grade=g, subject=s, topic=t)
            if not items and t:
                # try topic-only (ignore grade/subject)
                items = self._filter_items(grade=None, subject=None, topic=t)
            if not items:
                # final fallback: entire library
                logger.warning(
                    "No topic-based items for grade=%s subject=%s topic=%s; "
                    "falling back to entire library",
                    g,
                    s,
                    t,
                )
                items = all_items[:]

        # At this point, items is guaranteed non-empty if library has anything.
        if not items:
            # Extremely defensive; should basically never happen.
            raise ToolError("No matching items after fallbacks")

        # ---- pick next after last_question_id (within filtered list) ----
        if last_question_id:
            try:
                last_idx = next(
                    i
                    for i, it in enumerate(items)
                    if str(it.get("id")) == str(last_question_id)
                )
                nxt_idx = (last_idx + 1) % len(items)
            except StopIteration:
                nxt_idx = 0
        else:
            nxt_idx = 0

        nxt = items[nxt_idx]
        index_in_lesson = nxt_idx + 1
        total_in_lesson = len(items)
        has_more = total_in_lesson > 0 and index_in_lesson < total_in_lesson

        # lock the active lesson for this session (for completion tracking)
        active_lesson_id = effective_lesson_id or nxt.get("lesson_id")
        if active_lesson_id:
            if (
                sess.active_lesson_id
                and sess.active_lesson_id != active_lesson_id
            ):
                # new lesson in same WS session: reset tracking
                sess.answered_ids_for_lesson.clear()
            sess.active_lesson_id = active_lesson_id

        # First question in this session? Update profile's last_* metadata.
        # Use subject/topic from args if provided, otherwise from the question item.
        subj_for_tracking = s or nxt.get("subject")
        topic_for_tracking = t or nxt.get("topic")

        first_in_session = sess.seq == 0
        if first_in_session and sess.user_email and sess.profile_id:
            try:
                profile_store.update_profile_last_used(
                    email=sess.user_email,
                    profile_id=sess.profile_id,
                    subject=subj_for_tracking,
                    topic=topic_for_tracking,
                )
            except Exception as e:
                logger.warning(
                    "Failed to update profile last_used metadata: %s", e
                )

        sess.seq += 1

        # decorate and store as current question
        q_payload = self._to_question_payload(
            nxt,
            seq=sess.seq,
            index_in_lesson=index_in_lesson,
            total_in_lesson=total_in_lesson,
            has_more=has_more,
        )
        # keep canonical answer only server-side (omit from /live/current_question)
        q_payload["_answer"] = nxt.get("answer")
        sess.current_question = q_payload

        logger_agent.log_event(
            {
                "type": "live_tool_get_question",
                "filters": {
                    "grade": g or nxt.get("grade"),
                    "subject": subj_for_tracking,
                    "topic": topic_for_tracking,
                    "lesson_id": active_lesson_id,
                    "last_question_id": last_question_id,
                },
                "chosen_id": nxt.get("id"),
                "question": q_payload["question_text"],
                "session_id": session_id,
                "seq": sess.seq,
                "profile_id": sess.profile_id,
                "user_email": sess.user_email,
            }
        )
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

        Also enforces a guardrail against hallucinated / invalid question_id
        values and forwards rich attempt metadata to the gradebook
        (including lesson_id for per-card progress).

        NEW:
        - Computes whether the current lesson (question_set) is fully covered
          in THIS live session, and returns `lesson_done` in the result.
        """
        # --- GUARDRAIL: block hallucinated IDs like "None", "", etc. ---
        qid_str = str(question_id).strip()
        if qid_str.lower() in ("none", "null", "", "undefined"):
            logger.warning(
                "Blocked hallucinated update_answer with id=%r (session=%s, answer=%r)",
                question_id,
                session_id,
                user_answer,
            )
            return {
                "ok": False,
                "error": "invalid_id",
                "feedback": (
                    "SYSTEM: update_answer was called without a valid question_id. "
                    "You MUST call get_question() first and then reuse its question_id."
                ),
            }
        # ---------------------------------------------------------------

        sess = _get_session(session_id)

        # Idempotency
        if attempt_id and attempt_id in sess.processed_attempt_ids:
            return {
                "duplicate": True,
                "question_id": question_id,
                "session_id": session_id,
            }
        if attempt_id:
            sess.processed_attempt_ids.add(attempt_id)

        # Journal attempt (in-memory, per live run)
        att = Attempt(
            question_id=str(question_id),
            choice_index=choice_index,
            choice_text=str(user_answer),
            correct=bool(is_correct),
            attempt_id=attempt_id or f"ts-{int(time.time())}",
        )
        sess.last_answer = att
        sess.progress_log.append(att)

        # Observability (black-box log)
        logger_agent.log_event(
            {
                "type": "live_tool_update_answer",
                "question_id": question_id,
                "user_answer": user_answer,
                "is_correct": bool(is_correct),
                "session_id": session_id,
                "profile_id": sess.profile_id,
                "user_email": sess.user_email,
            }
        )

        # ---------- Look up content item once (for both gradebook + lesson_done) ----------
        items = library.ITEMS or []
        item = next(
            (it for it in items if str(it.get("id")) == str(question_id)),
            {},
        ) or {}

        lesson_id = item.get("lesson_id")
        subject = item.get("subject")
        topic = item.get("topic")

        # Gradebook: write attempt to Firestore if we know user + profile
        email = sess.user_email
        profile_id = sess.profile_id

        if email and profile_id:
            attempt_payload = {
                "question_id": str(question_id),
                "is_correct": bool(is_correct),
                "user_answer": str(user_answer),
                "topic": topic,
                "difficulty": item.get("difficulty"),
                "question_text": item.get("question_text"),
                # used for per-lesson stats + last_subject/last_topic
                "lesson_id": lesson_id,
                "subject": subject,
            }

            try:
                profile_store.get_or_create_session(
                    email=email,
                    profile_id=profile_id,
                    session_id=session_id,
                )
                profile_store.record_attempt(
                    email=email,
                    profile_id=profile_id,
                    session_id=session_id,
                    attempt_data=attempt_payload,
                )
            except Exception as e:
                logger.warning("Failed to write attempt to gradebook: %s", e)

        # Track answered IDs for active lesson in this live run
        if lesson_id and sess.active_lesson_id and sess.active_lesson_id == lesson_id:
            sess.answered_ids_for_lesson.add(str(question_id))

        # ---------- Detect "lesson complete in this session" ----------
        lesson_done = False
        lesson_total = None
        lesson_answered = None

        if lesson_id:
            # All questions that belong to this lesson_id in the content library
            lesson_qids = [
                str(it.get("id"))
                for it in items
                if it.get("lesson_id") == lesson_id
            ]

            if lesson_qids:
                lesson_qid_set = set(lesson_qids)

                # Which of those have been answered in THIS live session?
                # Prefer the answered_ids_for_lesson set, but intersect with real lesson_qids.
                if sess.answered_ids_for_lesson:
                    answered_ids = {
                        qid for qid in sess.answered_ids_for_lesson
                        if qid in lesson_qid_set
                    }
                else:
                    # fallback via progress_log if set is empty for some reason
                    answered_ids = {
                        a.question_id
                        for a in sess.progress_log
                        if a.question_id in lesson_qid_set
                    }

                lesson_total = len(lesson_qids)
                lesson_answered = len(answered_ids)

                # Simple rule: once we've answered every question in the set at least once,
                # mark the lesson as "done" for this session.
                if lesson_answered >= lesson_total:
                    lesson_done = True

        # ---------- Build result payload for the model + UI ----------
        result: Dict[str, Any] = {
            "ok": True,
            "correct": bool(is_correct),
            "feedback": "Great job!" if is_correct else "Not quite—try again.",
        }

        if lesson_done:
            result.update(
                {
                    "lesson_done": True,
                    "lesson_id": lesson_id,
                    "lesson_total": lesson_total,
                    "lesson_answered": lesson_answered,
                }
            )

        # Do NOT mutate sess.current_question here.
        # Do NOT return current_question here.
        return result
