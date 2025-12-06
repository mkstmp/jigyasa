# backend/content/sync_lessons_to_firestore.py
from __future__ import annotations

import json
from pathlib import Path

from backend.shared.database import get_firestore_client  # uses your existing config


def load_lessons_from_json() -> list[dict]:
    """Load lessons from backend/content/lessons.json.

    Supports either:
      - { "lessons": [ ... ] }
      - [ ... ]
    """
    path = Path(__file__).resolve().parent / "lessons.json"
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)

    if isinstance(data, dict) and isinstance(data.get("lessons"), list):
        lessons = data["lessons"]
    elif isinstance(data, list):
        lessons = data
    else:
        raise SystemExit(
            "lessons.json should be a list or an object with a 'lessons' array."
        )

    cleaned: list[dict] = []
    for raw in lessons:
        if not isinstance(raw, dict):
            continue
        lid = raw.get("id") or raw.get("lesson_id")
        if not lid:
            print("⚠️  Skipping lesson without id/lesson_id:", raw)
            continue

        doc = dict(raw)
        # Make sure lesson_id is always present and matches the doc id
        doc.setdefault("lesson_id", lid)
        cleaned.append(doc)

    return cleaned


def sync_lessons():
    db = get_firestore_client()
    lessons = load_lessons_from_json()

    batch = db.batch()
    count = 0

    for lesson in lessons:
        lid = lesson["lesson_id"]
        ref = db.collection("lessons").document(lid)

        # merge=True so we don't accidentally wipe extra Firestore-only fields
        batch.set(ref, lesson, merge=True)
        count += 1

        # Commit periodically to avoid oversized batches
        if count % 400 == 0:
            batch.commit()
            batch = db.batch()

    batch.commit()
    print(f"✅ Synced {count} lessons to Firestore.")


if __name__ == "__main__":
    sync_lessons()

