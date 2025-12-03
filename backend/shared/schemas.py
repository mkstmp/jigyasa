from __future__ import annotations

from typing import List, Optional, Dict, Any, Literal
from pydantic import BaseModel, Field


# Human-friendly labels for each lesson / collection.
# Single source of truth so we don't repeat titles in every question row.
LESSON_TITLES: dict[str, str] = {
    "kg_math_numbers_1to5": "Numbers 1–5",
    "kg_math_numbers_6to10": "Numbers 6–10",
    "kg_math_add_1digit_sum5": "Add numbers (sum ≤ 5)",
    "kg_math_compare_moreless": "Compare: more vs less",
}

LESSON_SUBTITLES: dict[str, str] = {
    "kg_math_numbers_1to5": "Count and compare objects from 1 to 5.",
    "kg_math_numbers_6to10": "Count and compare objects from 6 to 10.",
    "kg_math_add_1digit_sum5": "Add small numbers, with answers up to 5.",
    "kg_math_compare_moreless": "Decide which group has more or fewer.",
}


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
    grade: Optional[str] = None       # "KG", "Class-1"
    subject: Optional[str] = None     # "Math", "English"

    topic: str                        # "place-value-logic"
    subtopic: Optional[str] = None    # "number-riddles"
    difficulty: str                   # "olympiad-c1"

    # Collection / Lesson grouping for cards
    # e.g. one card/lesson = one collection_id
    collection_id: Optional[str] = None          # "kg_math_numbers_l1"
    collection_title: Optional[str] = None       # "Counting 1–10"
    collection_order: Optional[int] = None       # sort within a topic

    # Unified question text the tutor will speak/show
    question_text: str

    # "mcq" | "open" | "list"
    type: Literal["mcq", "open", "list"] = "open"

    # MCQ options (only for type="mcq")
    choices: Optional[List[str]] = None

    # Canonical answer (optional; useful for analytics or deterministic checks)
    answer: Optional[str] = None

    # Hints the AI can use if the child is stuck
    hints: List[str] = Field(default_factory=list)

    # Optional supports
    tags: List[str] = Field(default_factory=list)
    media: Optional[Media] = None

    # Only for list-type items
    list_spec: Optional[ListSpec] = None

    # Future-proof metadata
    explanation: Optional[str] = None   # For parent reports / “show solution”
    active: bool = True                 # To hide broken/experimental questions


# ---------- Tool payloads (for type hints & logs) ----------
class ToolGetQuestion(BaseModel):
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
