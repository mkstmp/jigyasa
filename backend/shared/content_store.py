# backend/shared/content_store.py
from __future__ import annotations

from typing import List, Dict, Any, Optional
from pathlib import Path
import json
import os
import logging

from backend.shared.database import get_firestore_client

log = logging.getLogger("jigyasa.content_store")

BASE_DIR = Path(__file__).resolve().parents[1]      # backend/
CONTENT_DIR = BASE_DIR / "content"
ITEMS_PATH = CONTENT_DIR / "content_new.json"
LESSONS_PATH = CONTENT_DIR / "lessons.json"


class ContentStore:
    def list_items(self) -> List[Dict[str, Any]]:
        raise NotImplementedError

    def list_lessons(self) -> List[Dict[str, Any]]:
        raise NotImplementedError


class JsonContentStore(ContentStore):
    """
    Uses content_new.json and lessons.json on disk.
    Good for local/dev and as a seed for Firestore.
    """

    def __init__(self) -> None:
        self._items_cache: Optional[List[Dict[str, Any]]] = None
        self._lessons_cache: Optional[List[Dict[str, Any]]] = None

    def _load_json_array(self, path: Path, key: str) -> List[Dict[str, Any]]:
        if not path.exists():
            log.warning("JsonContentStore: %s not found", path)
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            log.error("JsonContentStore: failed to load %s: %s", path, e)
            return []

        if isinstance(data, dict):
            arr = data.get(key) or data.get(key.rstrip("s")) or []
        elif isinstance(data, list):
            arr = data
        else:
            arr = []

        return [it for it in arr if isinstance(it, dict)]

    def list_items(self) -> List[Dict[str, Any]]:
        if self._items_cache is None:
            self._items_cache = self._load_json_array(ITEMS_PATH, "items")
        # return a shallow copy so callers can't mutate cache
        return list(self._items_cache)

    def list_lessons(self) -> List[Dict[str, Any]]:
        if self._lessons_cache is None:
            self._lessons_cache = self._load_json_array(LESSONS_PATH, "lessons")

        lessons: List[Dict[str, Any]] = []
        for raw in self._lessons_cache:
            lid = raw.get("id") or raw.get("lesson_id")
            if not lid:
                continue
            lesson = dict(raw)
            lesson.setdefault("id", lid)
            lesson.setdefault("lesson_id", lid)
            lessons.append(lesson)
        return lessons


class FirestoreContentStore(ContentStore):
    """
    Firestore layout:

      collection("lessons") / {lesson_id} doc:
          grade, subject, topic, title, subtitle, icon, badge,
          tags, total_questions, estimated_minutes, image_url, active, ...

      subcollection "questions" under each lesson doc:
          each document is one ContentItem with id question_id.
    """

    def __init__(self, client=None) -> None:
        self.db = client or get_firestore_client()

    def list_lessons(self) -> List[Dict[str, Any]]:
        lessons: List[Dict[str, Any]] = []
        for doc in self.db.collection("lessons").stream():
            data = doc.to_dict() or {}
            lid = data.get("lesson_id") or doc.id
            data["id"] = lid
            data.setdefault("lesson_id", lid)
            lessons.append(data)
        return lessons

    def list_items(self) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        lessons_coll = self.db.collection("lessons")

        for lesson_doc in lessons_coll.stream():
            lesson = lesson_doc.to_dict() or {}
            lid = lesson.get("lesson_id") or lesson_doc.id
            grade = lesson.get("grade")
            subject = lesson.get("subject")
            topic = lesson.get("topic")

            q_coll = lesson_doc.reference.collection("questions")
            for q_doc in q_coll.stream():
                data = q_doc.to_dict() or {}
                qid = data.get("id") or q_doc.id
                data["id"] = qid
                data.setdefault("lesson_id", lid)
                if grade is not None:
                    data.setdefault("grade", grade)
                if subject is not None:
                    data.setdefault("subject", subject)
                if topic is not None:
                    data.setdefault("topic", topic)
                items.append(data)

        return items


_backend_singleton: Optional[ContentStore] = None


def get_content_store() -> ContentStore:
    """
    Choose backend based on env:

      CONTENT_BACKEND=firestore  -> FirestoreContentStore
      anything else (or fallback) -> JsonContentStore
    """
    global _backend_singleton
    if _backend_singleton is not None:
        return _backend_singleton

    backend = os.getenv("CONTENT_BACKEND", "json").strip().lower()
    if backend == "firestore":
        try:
            _backend_singleton = FirestoreContentStore()
            log.info("ContentStore: using Firestore backend")
            return _backend_singleton
        except Exception as e:
            log.error(
                "ContentStore: failed to init Firestore backend (%s), "
                "falling back to JSON",
                e,
            )

    _backend_singleton = JsonContentStore()
    log.info("ContentStore: using JSON backend from %s", CONTENT_DIR)
    return _backend_singleton
