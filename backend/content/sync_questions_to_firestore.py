# backend/content/sync_questions_to_firestore.py
from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List, Any

from backend.shared.database import get_firestore_client  # uses your existing config


def _load_questions_from_kg_dir() -> Dict[str, List[Dict[str, Any]]]:
    """
    Load questions from per-lesson JSON files under backend/content/KG.

    Supports either:
      - { "items": [ { ...question... }, ... ] }
      - [ { ...question... }, ... ]

    Returns:
        { lesson_id: [ question_dict, ... ], ... }
    """
    base = Path(__file__).resolve().parent / "KG"
    if not base.is_dir():
        raise SystemExit(f"KG directory not found at {base}")

    lessons: Dict[str, List[Dict[str, Any]]] = {}

    for path in sorted(base.glob("*.json")):
        try:
            with path.open("r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            print(f"⚠️  Skipping {path.name}: failed to load JSON ({e})")
            continue

        if isinstance(data, dict) and isinstance(data.get("items"), list):
            items = data["items"]
        elif isinstance(data, list):
            items = data
        else:
            print(
                f"⚠️  Skipping {path.name}: expected list or object with 'items' array."
            )
            continue

        if not items:
            print(f"⚠️  {path.name}: no items found.")
            continue

        for raw in items:
            if not isinstance(raw, dict):
                continue

            qid = str(raw.get("id") or "").strip()
            if not qid:
                print(f"  ⚠️  {path.name}: skipping question without 'id': {raw}")
                continue

            lid = str(raw.get("lesson_id") or "").strip()
            if not lid:
                # Fallback: infer from filename
                lid = path.stem
                raw["lesson_id"] = lid

            # Normalize some basics
            raw["id"] = qid
            if "active" not in raw:
                raw["active"] = True

            lessons.setdefault(lid, []).append(raw)

    return lessons


def _sync_to_firestore(questions_by_lesson: Dict[str, List[Dict[str, Any]]]) -> None:
    """
    Write questions into Firestore under:
        lessons/{lesson_id}/questions/{question_id}

    - Upserts docs for all questions in JSON.
    - Deletes any question docs in that subcollection that are *not*
      present in the corresponding JSON for that lesson.
    """
    if not questions_by_lesson:
        print("No questions loaded; nothing to sync.")
        return

    db = get_firestore_client()
    lessons_ref = db.collection("lessons")

    total_written = 0
    lesson_count = 0

    for lesson_id, questions in questions_by_lesson.items():
        lesson_count += 1
        qcol = lessons_ref.document(lesson_id).collection("questions")

        # Fetch existing doc IDs for this lesson's questions
        existing_ids = [doc.id for doc in qcol.stream()]
        new_ids = {str(q["id"]) for q in questions}

        # Delete questions that are no longer in JSON
        to_delete = [qid for qid in existing_ids if qid not in new_ids]
        for qid in to_delete:
            print(f"  🗑️  Deleting question {qid} under lesson {lesson_id}")
            qcol.document(qid).delete()

        # Upsert all questions from JSON
        for q in questions:
            qid = str(q["id"])
            data: Dict[str, Any] = dict(q)
            # Ensure 'id' field matches doc id
            data["id"] = qid
            qcol.document(qid).set(data)
            total_written += 1

        print(
            f"✅ Lesson {lesson_id}: synced {len(questions)} questions "
            f"(deleted {len(to_delete)})."
        )

    print(
        f"\n✅ Done. Synced {total_written} questions "
        f"across {lesson_count} lesson(s)."
    )


def main() -> None:
    questions_by_lesson = _load_questions_from_kg_dir()
    print(
        f"Loaded questions for {len(questions_by_lesson)} lesson(s) "
        f"from backend/content/KG."
    )
    _sync_to_firestore(questions_by_lesson)


if __name__ == "__main__":
    main()

