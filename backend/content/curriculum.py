# backend/content/curriculum.py
from __future__ import annotations

from typing import List, Dict, Any, Optional
import logging

from backend.shared.content_store import get_content_store

log = logging.getLogger("jigyasa.curriculum")

_store = get_content_store()

# Raw list of lesson metadata from the content store
CURRICULUM: List[Dict[str, Any]] = _store.list_lessons()


def reload_curriculum() -> None:
    """
    Optional dev helper to refresh lessons from backend.
    """
    global CURRICULUM
    CURRICULUM = _store.list_lessons()
    log.info("curriculum: reloaded %d lessons", len(CURRICULUM))


# --------------------------------------------------------------------
# Annotation helpers (same as your existing logic)
# --------------------------------------------------------------------
def _annotate_lesson(
    lesson: Dict[str, Any],
    lesson_stats: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Add status / completion fields based on per-lesson stats."""
    base = dict(lesson)

    stats = lesson_stats or {}
    attempted = int(stats.get("attempted", 0))
    correct = int(stats.get("correct", 0))
    mastered = bool(stats.get("mastered", False))

    total_qs = int(base.get("total_questions", 10))
    completion_pct = 0
    if attempted > 0 and total_qs > 0:
        completion_pct = int(
            min(100, round(min(attempted, total_qs) / total_qs * 100))
        )

    if mastered:
        status = "done"
    elif attempted > 0:
        status = "in_progress"
    else:
        status = "new"

    base.update(
        {
            "status": status,  # "new" | "in_progress" | "done"
            "completion_pct": completion_pct,
            "attempted": attempted,
            "correct": correct,
        }
    )
    return base


def get_lessons(
    *,
    grade: str,
    subject: str,
    lesson_stats_map: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """
    Filter curriculum for a grade+subject and annotate each lesson with
    status and completion_pct based on profile.lesson_stats (if provided).

    lesson_stats_map is expected to be:
        { lesson_id: {attempted, correct, mastered, ...}, ... }

    IMPORTANT: we use lesson.lesson_id **or** lesson.id as the key,
    so it still works even if the card ID and the stored stats key differ.
    """
    lesson_stats_map = lesson_stats_map or {}
    results: List[Dict[str, Any]] = []

    for lesson in CURRICULUM:
        if lesson.get("grade") != grade:
            continue
        if lesson.get("subject") != subject:
            continue

        lid = lesson.get("lesson_id") or lesson.get("id")
        stats = lesson_stats_map.get(lid, {})
        results.append(_annotate_lesson(lesson, stats))

    return results
