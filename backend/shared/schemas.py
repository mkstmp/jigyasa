# backend/shared/schemas.py
from __future__ import annotations

from typing import List, Optional, Dict, Any, Literal
from pydantic import BaseModel, Field


# ---------- Media (optional) ----------
class MediaImage(BaseModel):
    url: str                   # "/static/img/circle.svg" or https://...
    alt: Optional[str] = None  # accessibility hint


class Media(BaseModel):
    image: Optional[MediaImage] = None


# ---------- List-question spec (only when type="list") ----------
class ListSpec(BaseModel):
    required: int = 5                    # how many items the child should list
    category: str = "items"              # e.g., "flowers", "animals"
    allowed: Optional[List[str]] = None  # whitelist (closed-world) or None=open-world


# ---------- Core content item ----------
class ContentItem(BaseModel):
    id: str
    topic: str
    subtopic: Optional[str] = None
    difficulty: str

    # Unified question text the tutor will speak/show
    question_text: str

    # one of: "mcq" | "open" | "list"
    type: Literal["mcq", "open", "list"]

    # MCQ options (only for type="mcq")
    choices: Optional[List[str]] = None

    # Canonical answer (optional; useful for analytics or deterministic checks)
    answer: Optional[str] = None

    # Optional supports
    tags: List[str] = Field(default_factory=list)
    media: Optional[Media] = None

    # Only for list-type items
    list_spec: Optional[ListSpec] = None


# ---------- Tool payloads (for type hints & logs) ----------
class ToolGetQuestion(BaseModel):
    grade: str
    subject: str
    topic: str
    last_question_id: Optional[str] = None


class ToolUpdateAnswer(BaseModel):
    question_id: str
    user_answer: str                  # model's transcript/normalized answer
    is_correct: bool                  # model's judgment
    transcript: Optional[str] = None  # optional raw transcript if you want it


# ---------- Events / profiling ----------
class EventRecord(BaseModel):
    profile_id: str
    session_id: Optional[str] = None
    type: str  # e.g., "live_tool_get_question", "live_tool_update_answer", "hint_requested", etc.
    extra: Dict[str, Any] = Field(default_factory=dict)


class ProfileState(BaseModel):
    profile_id: str
    streak: int = 0
    weak_topics: List[str] = Field(default_factory=list)
    seen_ids: List[str] = Field(default_factory=list)
    last_10: List[Dict[str, Any]] = Field(default_factory=list)
    attempted: int = 0
    correct: int = 0


class User(BaseModel):
    name: str
    age: int
    grade: str
    language: Optional[str] = None  # e.g., "en-IN"
