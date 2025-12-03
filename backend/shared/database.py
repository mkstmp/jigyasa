# backend/shared/db.py
from __future__ import annotations

from google.cloud import firestore
from functools import lru_cache
from backend.shared import config

@lru_cache(maxsize=1)
def get_firestore_client() -> firestore.Client:
    # Uses GOOGLE_APPLICATION_CREDENTIALS + GCP_PROJECT_ID from env/config
    return firestore.Client(project=config.GCP_PROJECT_ID)
