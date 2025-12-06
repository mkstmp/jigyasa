# backend/content/library.py
from __future__ import annotations

from typing import List, Dict, Any
import logging

from backend.shared.content_store import get_content_store

log = logging.getLogger("jigyasa.library")

_store = get_content_store()

# Flat list of all ContentItem dicts (what ToolRouter uses)
ITEMS: List[Dict[str, Any]] = _store.list_items()


def reload_items() -> None:
    """
    Optional helper if you ever want to hot-reload content
    without restarting the server (e.g. dev endpoints).
    """
    global ITEMS
    ITEMS = _store.list_items()
    log.info("library: reloaded %d items", len(ITEMS))
