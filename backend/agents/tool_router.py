# backend/agents/tool_router.py
from typing import Dict, Any, Optional, List
from backend.content import library
from backend.agents import logger_agent

class ToolError(Exception):
    pass


class ToolRouter:
    """
    Shared tool logic for:
      - get_question (first or next after last_question_id)
      - update_answer (log/record child's answer; model judges correctness)
    """

    def __init__(self):
        # If you wire session/profile later, you can keep per-session state here.
        pass

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

        # Topic filter (present in current catalog)
        if tp:
            items = [it for it in items if self._norm(it.get("topic")) == tp]

        # Subject / grade filters (no-op if your catalog doesn’t have these fields yet)
        if sj:
            items = [it for it in items if self._norm(it.get("subject")) in ("", sj, None)]
        if tg:
            items = [it for it in items if self._norm(it.get("grade")) in ("", tg, None)]

        return items

    def _to_question_payload(self, it: Dict[str, Any]) -> Dict[str, Any]:
        """
        Normalize a library item to the tool response shape expected by the LLM.
        Compatible with both the old catalog (prompt_visual/prompt_tts/hints)
        and the new schema (question_text, list_spec).
        """
        # Prefer new field; fall back to old fields
        question_text = (
            it.get("question_text")
            or it.get("prompt_tts")
            or it.get("prompt_visual")
            or ""
        )

        payload: Dict[str, Any] = {
            "question_id": it.get("id") or "",
            "type": it.get("type") or "",                # "mcq" | "open" | "list"
            "question_text": question_text,              # <-- new canonical field
            "choices": list(it.get("choices") or []),    # always a list
            "answer": it.get("answer"),                  # string or None
            "topic": it.get("topic") or "",
            "difficulty": it.get("difficulty") or "",
        }

        # Transitional compatibility: include deprecated "prompt" for any
        # prompts the model might already be relying on.
        payload["prompt"] = question_text  # DEPRECATED: kept for safety

        # Pass through list_spec when present (list questions only)
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
        last_question_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Fetch a question from the local content library.
        - If last_question_id is None: return the first matching question
        - Else: return the next question after that id within the filtered set
        - If filters produce no results, relax them progressively, then fall back to first item.
        """
        all_items: List[Dict[str, Any]] = library.ITEMS or []
        if not all_items:
            raise ToolError("No items in library")

        # 1) strict filter
        items = self._filter_items(grade=grade, subject=subject, topic=topic)

        # 2) relax subject/grade if needed
        if not items:
            items = self._filter_items(grade=None, subject=None, topic=topic)

        # 3) final fallback to full catalog
        if not items:
            items = all_items[:]

        if not items:
            raise ToolError("No matching items")

        # pick next after last_question_id (inside the filtered list)
        if last_question_id:
            try:
                idx = next(i for i, it in enumerate(items) if it.get("id") == last_question_id)
                nxt = items[(idx + 1) % len(items)]
            except StopIteration:
                nxt = items[0]
        else:
            nxt = items[0]

        # Log for observability
        logger_agent.log_event({
            "type": "live_tool_get_question",
            "filters": {"grade": grade, "subject": subject, "topic": topic, "last_question_id": last_question_id},
            "chosen_id": nxt.get("id"),
        })

        return self._to_question_payload(nxt)

    def update_answer(self, *, question_id: str, user_answer: str, is_correct: bool) -> Dict[str, Any]:
        """
        Record/log the child's answer. The model is the grader in v4.
        Extend here to mutate profile state (streaks, difficulty, weak_topics, etc.).
        """
        logger_agent.log_event({
            "type": "live_tool_update_answer",
            "question_id": question_id,
            "user_answer": user_answer,
            "is_correct": is_correct,
        })
        return {"ok": True}
