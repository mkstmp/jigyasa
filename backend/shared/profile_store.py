# backend/shared/profile_store.py
from __future__ import annotations

from typing import Dict, Any, List, Optional
from google.cloud import firestore

from backend.shared.database import get_firestore_client


class FirestoreProfileStore:
    """
    Manages the 'Gradebook' hierarchy:

        users/{email}/profiles/{pid}/sessions/{sid}/attempts/{aid}

    - Users + profiles: who is who.
    - Sessions: one learning session (e.g., one live call).
    - Attempts: each answered question.

    Also keeps per-profile aggregates + per-lesson aggregates on the profile doc:

        users/{email}/profiles/{pid}:
          stats: {
            attempted: int,
            correct:   int,
            streak:    int,       # optional / future
          }
          lesson_stats: {
            <lesson_id>: {
              attempted:    int,
              correct:      int,
              mastered:     bool,
              subject:      str,
              topic:        str,
              last_used_at: Timestamp,
            },
            ...
          }
    """

    def __init__(self) -> None:
        self.client: firestore.Client = get_firestore_client()

    # ---------- Path Helpers ----------

    def _user_doc(self, email: str) -> firestore.DocumentReference:
        return self.client.collection("users").document(email)

    def _profiles_col(self, email: str) -> firestore.CollectionReference:
        return self._user_doc(email).collection("profiles")

    def _profile_doc(self, email: str, profile_id: str) -> firestore.DocumentReference:
        return self._profiles_col(email).document(profile_id)

    def _sessions_col(self, email: str, profile_id: str) -> firestore.CollectionReference:
        return self._profile_doc(email, profile_id).collection("sessions")

    def _session_doc(self, email: str, profile_id: str, session_id: str) -> firestore.DocumentReference:
        return self._sessions_col(email, profile_id).document(session_id)

    def _attempts_col(self, email: str, profile_id: str, session_id: str) -> firestore.CollectionReference:
        return self._session_doc(email, profile_id, session_id).collection("attempts")

    # ---------- Users & Profiles (existing behavior) ----------

    def ensure_user(self, email: str, name: Optional[str], picture: Optional[str]) -> None:
        self._user_doc(email).set(
            {
                "name": name,
                "picture": picture,
            },
            merge=True,
        )

    def list_profiles(self, email: str) -> List[Dict[str, Any]]:
        docs = self._profiles_col(email).stream()
        items: List[Dict[str, Any]] = []
        for d in docs:
            data = d.to_dict() or {}
            data["id"] = d.id
            items.append(data)
        return items

    def get_profile(self, email: str, profile_id: str) -> Optional[Dict[str, Any]]:
        snap = self._profile_doc(email, profile_id).get()
        if not snap.exists:
            return None
        data = snap.to_dict() or {}
        data["id"] = snap.id
        return data

    def upsert_profile(self, email: str, profile: Dict[str, Any]) -> None:
        self._profile_doc(email, profile["id"]).set(profile, merge=True)

    def delete_profile(self, email: str, profile_id: str) -> bool:
        doc = self._profile_doc(email, profile_id)
        if not doc.get().exists:
            return False
        doc.delete()
        return True

    def update_profile_last_used(
        self,
        *,
        email: str,
        profile_id: str,
        subject: Optional[str] = None,
        topic: Optional[str] = None,
    ) -> None:
        """
        Update per-profile metadata when a session is used.

        Writes:
          - last_subject
          - last_topic
          - last_used_at (SERVER_TIMESTAMP)
        """
        if not email or not profile_id:
            return

        doc_ref = self._profile_doc(email, profile_id)

        payload: Dict[str, Any] = {
            "last_used_at": firestore.SERVER_TIMESTAMP,
        }
        if subject:
            payload["last_subject"] = subject
        if topic:
            payload["last_topic"] = topic

        doc_ref.set(payload, merge=True)

    # ---------- Sessions & Attempts (Gradebook logic) ----------

    def get_or_create_session(self, email: str, profile_id: str, session_id: str) -> None:
        """Ensure a session doc exists with sane defaults."""
        doc_ref = self._session_doc(email, profile_id, session_id)
        doc_ref.set(
            {
                "started_at": firestore.SERVER_TIMESTAMP,
                "device": "web",
                "attempted": 0,
                "correct": 0,
                "streak": 0,
            },
            merge=True,
        )

    def record_attempt(
        self,
        *,
        email: str,
        profile_id: str,
        session_id: str,
        attempt_data: Dict[str, Any],
    ) -> None:
        if not session_id:
            return

        # 1) Add attempt document
        self._attempts_col(email, profile_id, session_id).add(
            {
                **attempt_data,
                "created_at": firestore.SERVER_TIMESTAMP,
            }
        )

        is_correct = bool(attempt_data.get("is_correct", False))
        lesson_id = attempt_data.get("lesson_id")
        subject = attempt_data.get("subject")
        topic = attempt_data.get("topic")

        # 2) Update session aggregates
        session_ref = self._session_doc(email, profile_id, session_id)
        snap = session_ref.get()
        sdata = snap.to_dict() or {}
        attempted = int(sdata.get("attempted", 0)) + 1
        correct = int(sdata.get("correct", 0)) + (1 if is_correct else 0)
        streak = int(sdata.get("streak", 0))
        streak = streak + 1 if is_correct else 0

        session_ref.set(
            {
                "attempted": attempted,
                "correct": correct,
                "streak": streak,
                "last_active": firestore.SERVER_TIMESTAMP,
            },
            merge=True,
        )

        # 3) Calculate Profile Stats
        profile_ref = self._profile_doc(email, profile_id)
        psnap = profile_ref.get()
        pdata = psnap.to_dict() or {}
        stats = pdata.get("stats", {}) or {}
        p_attempted = int(stats.get("attempted", 0)) + 1
        p_correct = int(stats.get("correct", 0)) + (1 if is_correct else 0)

        # 4) Prepare the update payload
        # KEY CHANGE: We construct a dictionary for .update()
        update_payload = {
            "stats.attempted": p_attempted,
            "stats.correct": p_correct,
            "last_used_at": firestore.SERVER_TIMESTAMP
        }

        if subject:
            update_payload["last_subject"] = subject
        if topic:
            update_payload["last_topic"] = topic

        # 5) Per-lesson stats
        if lesson_id:
            all_lesson_stats = pdata.get("lesson_stats", {}) or {}
            current = all_lesson_stats.get(lesson_id, {}) or {}

            ls_attempted = int(current.get("attempted", 0)) + 1
            ls_correct = int(current.get("correct", 0)) + (1 if is_correct else 0)

            accuracy = ls_correct / ls_attempted if ls_attempted > 0 else 0.0
            mastered = ls_attempted >= 8 and accuracy >= 0.8

            # Use dot notation for nested update
            update_payload[f"lesson_stats.{lesson_id}"] = {
                "attempted": ls_attempted,
                "correct": ls_correct,
                "mastered": mastered,
                "subject": subject or current.get("subject"),
                "topic": topic or current.get("topic"),
                "last_used_at": firestore.SERVER_TIMESTAMP,
            }

        try:
            # Use update() to respect dot notation
            profile_ref.update(update_payload)
        except Exception:
            # Fallback: If doc doesn't exist yet, we must create it structure manually
            full_doc = {
                "stats": {"attempted": p_attempted, "correct": p_correct},
                "last_used_at": firestore.SERVER_TIMESTAMP
            }
            if lesson_id:
                full_doc["lesson_stats"] = {
                    lesson_id: update_payload[f"lesson_stats.{lesson_id}"]
                }
            profile_ref.set(full_doc, merge=True)