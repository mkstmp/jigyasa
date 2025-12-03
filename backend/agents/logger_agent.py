# backend/agents/logger_agent.py
from __future__ import annotations

import logging
from typing import Dict, Any

from google.cloud import firestore
from backend.shared.database import get_firestore_client

log = logging.getLogger("jigyasa.events")


def log_event(event_data: Dict[str, Any]) -> None:
    """
    Writes raw system events to the top-level 'events' collection.

    This is the 'Black Box' for debugging:
    - We do NOT do any business logic here.
    - Gradebook / stats are handled elsewhere (profile_store + /events/record).
    """
    try:
        # 1) Mirror to console so you still see it in the terminal
        print(f"[EVENT] {event_data.get('type')} {event_data}", flush=True)
    except Exception:
        # Don't crash because print failed
        pass

    try:
        db = get_firestore_client()
    except Exception as e:
        log.error("Failed to get Firestore client for events: %s", e)
        return

    try:
        payload = dict(event_data)
        payload["created_at"] = firestore.SERVER_TIMESTAMP
        db.collection("events").add(payload)
    except Exception as e:
        # Never crash the app because logging failed
        log.error("Failed to log event to Firestore: %s", e)
