# backend/content/curriculum.py
from __future__ import annotations

from typing import List, Dict, Any, Optional
import json
import os
import logging

from backend.shared.schemas import LESSON_TITLES, LESSON_SUBTITLES


log = logging.getLogger("jigyasa.curriculum")

DATA_PATH = os.path.join(os.path.dirname(__file__), "content_new.json")


# --------------------------------------------------------------------
# Load curriculum dynamically from content_new.json (if present)
# --------------------------------------------------------------------
def _load_curriculum_from_file() -> List[Dict[str, Any]]:
    """
    Try to derive a curriculum list from content_new.json.

    Supports two common shapes:

    1) Top-level {"lessons": [ {id, grade, subject, topic, title, ...} ]}
    2) Top-level {"items": [ {..., lesson_id, grade, subject, topic, ...} ]}
       – we group items by lesson_id to form cards.
    """
    if not os.path.exists(DATA_PATH):
        log.warning("curriculum: DATA_PATH %s not found; using static fallback", DATA_PATH)
        return []

    try:
        with open(DATA_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        log.warning("curriculum: failed to load %s: %s; using static fallback", DATA_PATH, e)
        return []

    lessons: List[Dict[str, Any]] = []

    # Case 1: explicit lessons list
    if isinstance(data, dict) and isinstance(data.get("lessons"), list):
        for raw in data["lessons"]:
            if not isinstance(raw, dict):
                continue

            lid = raw.get("id") or raw.get("lesson_id")
            if not lid:
                continue

            items_list = raw.get("items") or raw.get("questions") or []
            total_qs = int(raw.get("total_questions") or len(items_list) or 10)

            lesson = {
                "id": lid,
                "lesson_id": lid,
                "grade": raw.get("grade") or "KG",
                "subject": raw.get("subject") or "Math",
                "topic": raw.get("topic") or "",
                "title": raw.get("title") or lid,
                "subtitle": raw.get("subtitle") or "",
                "icon": raw.get("icon") or "📘",
                "badge": raw.get("badge") or (raw.get("topic") or "").title() or "Lesson",
                "color": raw.get("color") or "#e5e7eb",
                "total_questions": total_qs,
                "estimated_minutes": int(raw.get("estimated_minutes") or 5),
            }
            lessons.append(lesson)

    # Case 2: only items list – group by lesson_id
    if not lessons and isinstance(data, dict) and isinstance(data.get("items"), list):
        grouped: Dict[str, Dict[str, Any]] = {}
        for it in data["items"]:
            
           
            if not isinstance(it, dict):
                continue
            lid = it.get("lesson_id")
            if not lid:
                continue

            topic = it.get("topic") or ""

            # Look up centralized schema labels
            nice_title = LESSON_TITLES.get(lid)
            nice_subtitle = LESSON_SUBTITLES.get(lid)

            bucket = grouped.setdefault(
                lid,
                {
                    "id": lid,
                    "lesson_id": lid,
                    "grade": it.get("grade") or "KG",
                    "subject": it.get("subject") or "Math",
                    "topic": topic,
                    # priority:
                    # 1) per-row override (lesson_title)
                    # 2) schema-level override (LESSON_TITLES)
                    # 3) fall back to lesson_id
                    "title": it.get("lesson_title") or nice_title or lid,
                    # same priority for subtitle
                    "subtitle": it.get("lesson_subtitle") or nice_subtitle or "",
                    "icon": it.get("lesson_icon") or "📘",
                    "badge": (topic or "").title() or "Lesson",
                    "color": "#e5e7eb",
                    "total_questions": 0,
                    "estimated_minutes": 5,
                },
            )

            bucket["total_questions"] = int(bucket.get("total_questions", 0)) + 1

        lessons = list(grouped.values())

    if not lessons:
        log.warning("curriculum: no lessons derived from %s; using static fallback", DATA_PATH)

    return lessons


# --------------------------------------------------------------------
# Static fallback if file missing / malformed
# --------------------------------------------------------------------
_FALLBACK_CURRICULUM: List[Dict[str, Any]] = [
    {
        "id": "kg_math_numbers_1to5",
        "lesson_id": "kg_math_numbers_1to5",
        "grade": "KG",
        "subject": "Math",
        "topic": "Numbers",
        "title": "Numbers 1–5",
        "subtitle": "Count and compare from 1 to 5.",
        "icon": "🔢",
        "badge": "Numbers",
        "color": "#dbeafe",  # light blue border
        "total_questions": 10,
        "estimated_minutes": 5,
    },
    {
        "id": "kg_math_numbers_6to10",
        "lesson_id": "kg_math_numbers_6to10",
        "grade": "KG",
        "subject": "Math",
        "topic": "Numbers",
        "title": "Numbers 6–10",
        "subtitle": "Count backwards and forwards to 10.",
        "icon": "🔟",
        "badge": "Numbers",
        "color": "#e0f2fe",
        "total_questions": 10,
        "estimated_minutes": 5,
    },
    {
        "id": "kg_math_add_tinysums",
        "lesson_id": "kg_math_add_tinysums",
        "grade": "KG",
        "subject": "Math",
        "topic": "Addition",
        "title": "Tiny Sums (≤ 5)",
        "subtitle": "1-digit addition with stories.",
        "icon": "➕",
        "badge": "Addition",
        "color": "#dcfce7",
        "total_questions": 10,
        "estimated_minutes": 5,
    },
    {
        "id": "kg_math_compare_moreorless",
        "lesson_id": "kg_math_compare_moreorless",
        "grade": "KG",
        "subject": "Math",
        "topic": "Comparison",
        "title": "More or Less?",
        "subtitle": "Compare who has more or less.",
        "icon": "⚖️",
        "badge": "Comparison",
        "color": "#fef3c7",
        "total_questions": 8,
        "estimated_minutes": 5,
    },
]


# Build final curriculum at import time
CURRICULUM: List[Dict[str, Any]] = _load_curriculum_from_file() or _FALLBACK_CURRICULUM


# --------------------------------------------------------------------
# Annotation helpers
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

        # Prefer explicit lesson_id, fall back to id
        lid = lesson.get("lesson_id") or lesson.get("id")
        stats = lesson_stats_map.get(lid, {})
        results.append(_annotate_lesson(lesson, stats))

    return results
