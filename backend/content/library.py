# backend/content/library.py
import json
import os
import random
from typing import List, Optional, Dict, Any

# Path to your JSON file (adjust name if needed)
DATA_PATH = os.path.join(os.path.dirname(__file__), "content_new.json")


def _load_items() -> List[Dict[str, Any]]:
    """Load content items from JSON once at startup."""
    if not os.path.exists(DATA_PATH):
        print(f"[content] DATA_PATH not found: {DATA_PATH}")
        return []

    try:
        with open(DATA_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)

        # Support both:
        #   { "items": [ ... ] }
        # and
        #   [ ... ]
        if isinstance(data, dict) and "items" in data:
            raw_items = data["items"]
        else:
            raw_items = data

        if isinstance(raw_items, list):
            return raw_items

        print("[content] JSON format not recognized; expected list or {items:[...]}")
        return []
    except Exception as e:
        print(f"[content] Error loading content library: {e}")
        return []


# Load once; ToolRouter uses this
ITEMS: List[Dict[str, Any]] = _load_items()


def fetch(
    grade: Optional[str],
    subject: Optional[str],
    topic: Optional[str],
    exclude_ids: Optional[List[str]] = None,
) -> Optional[Dict[str, Any]]:
    """
    Optional helper: filter ITEMS in-memory. ToolRouter currently
    does its own filtering; this is here for future use or scripts.
    """
    g = (grade or "").strip().lower()
    s = (subject or "").strip().lower()
    t = (topic or "").strip().lower()

    exclude_set = set(exclude_ids or [])

    candidates: List[Dict[str, Any]] = []
    for item in ITEMS:
        # Skip if excluded
        if item.get("id") in exclude_set:
            continue

        # Skip if explicitly inactive
        if item.get("active") is False:
            continue

        item_grade = (item.get("grade") or "").strip().lower()
        item_subject = (item.get("subject") or "").strip().lower()
        item_topic = (item.get("topic") or "").strip().lower()

        # Topic: usually strict
        if t and t != item_topic:
            continue

        # Subject: simple containment (e.g. "math" in "mathematics")
        if s and s not in item_subject:
            continue

        # Grade: simple equality for now (we can relax later)
        if g and g != item_grade:
            continue

        candidates.append(item)

    if candidates:
        return random.choice(candidates)

    # Fallback: if strict grade match fails, relax grade but keep subject/topic
    if t or s:
        relaxed: List[Dict[str, Any]] = []
        for item in ITEMS:
            if item.get("id") in exclude_set:
                continue
            if item.get("active") is False:
                continue

            item_subject = (item.get("subject") or "").strip().lower()
            item_topic = (item.get("topic") or "").strip().lower()

            if t and t != item_topic:
                continue
            if s and s not in item_subject:
                continue

            relaxed.append(item)

        if relaxed:
            return random.choice(relaxed)

    return None
