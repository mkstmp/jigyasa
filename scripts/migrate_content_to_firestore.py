# scripts/migrate_content_to_firestore.py
from __future__ import annotations

from pathlib import Path
import json

from backend.shared.database import get_firestore_client


ROOT = Path(__file__).resolve().parents[1]
CONTENT_DIR = ROOT / "backend" / "content"
LESSONS_PATH = CONTENT_DIR / "lessons.json"
ITEMS_PATH = CONTENT_DIR / "content_new.json"


def load_json(path: Path):
    data = json.loads(path.read_text(encoding="utf-8"))
    return data


def main() -> None:
    db = get_firestore_client()

    lessons_data = load_json(LESSONS_PATH)
    items_data = load_json(ITEMS_PATH)

    lessons = lessons_data.get("lessons", lessons_data) or []
    items = items_data.get("items", items_data) or []

    # group questions by lesson_id
    by_lesson = {}
    for it in items:
        if not isinstance(it, dict):
            continue
        lid = it.get("lesson_id")
        if not lid:
            continue
        by_lesson.setdefault(lid, []).append(it)

    for raw in lessons:
        if not isinstance(raw, dict):
            continue
        lid = raw.get("id") or raw.get("lesson_id")
        if not lid:
            continue

        # lesson doc fields (don't store duplicate id)
        lesson_doc = dict(raw)
        lesson_doc["lesson_id"] = lid
        lesson_doc.pop("id", None)

        print(f"Writing lesson {lid} …")
        lesson_ref = db.collection("lessons").document(lid)
        lesson_ref.set(lesson_doc)

        # questions under this lesson
        for q in by_lesson.get(lid, []):
            qid = q.get("id")
            if not qid:
                continue
            q_doc = dict(q)
            q_doc.pop("id", None)          # Firestore doc id is qid
            # keep lesson_id on the question too (handy for queries)
            q_doc["lesson_id"] = lid

            print(f"  ↳ question {qid}")
            lesson_ref.collection("questions").document(qid).set(q_doc)

    print("Done.")


if __name__ == "__main__":
    main()

