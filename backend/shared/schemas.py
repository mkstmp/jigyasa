from __future__ import annotations

from typing import List, Optional, Dict, Any, Literal
from pydantic import BaseModel, Field


# -------------------------------------------------------------------
# Media (for “what shape is this?” / “what animal is this?” etc.)
# -------------------------------------------------------------------
class MediaImage(BaseModel):
    url: str                   # "/static/img/circle.svg" or "https://..."
    alt: Optional[str] = None  # accessibility / screen-reader text


class Media(BaseModel):
    image: Optional[MediaImage] = None
    # Future: audio, sprite_id, animation_id, etc.


# -------------------------------------------------------------------
# List-question spec (only when type="list")
# -------------------------------------------------------------------
class ListSpec(BaseModel):
    required: int = 5                    # how many items the child should list
    category: str = "items"              # e.g., "flowers", "animals"
    allowed: Optional[List[str]] = None  # whitelist (closed-world) or None=open-world


# -------------------------------------------------------------------
# Core Question item (previously ContentItem)
# NOTE: hints + explanation are removed, media is kept.
# -------------------------------------------------------------------
class ContentItem(BaseModel):
    """
    Single atomic question. This is what we store in content.json, etc.
    Think of this as Question.
    """

    id: str

    # Placement in curriculum
    grade: Optional[str] = None       # "KG", "Class-1"
    subject: Optional[str] = None     # "Math", "English"
    topic: str                        # "numbers", "addition", "shapes"
    subtopic: Optional[str] = None    # finer grouping if needed
    difficulty: str                   # "kg-easy", "olympiad-c1", etc.

    # Lesson / collection grouping for cards
    collection_id: Optional[str] = None          # e.g. "kg_math_numbers_1to5"
    collection_title: Optional[str] = None       # optional override
    collection_order: Optional[int] = None       # sort within topic / lane

    # Unified question text the tutor will speak/show
    question_text: str

    # "mcq" | "open" | "list"
    type: Literal["mcq", "open", "list"] = "open"

    # MCQ options (only for type="mcq")
    choices: Optional[List[str]] = None

    # Canonical answer (stringified)
    answer: Optional[str] = None

    # Visual support for questions (“What shape is this?”, “What animal is this?”)
    media: Optional[Media] = None

    # Lightweight metadata for filtering, analytics, etc.
    tags: List[str] = Field(default_factory=list)

    # Only for list-type items
    list_spec: Optional[ListSpec] = None

    # To hide broken/experimental questions without deleting rows
    active: bool = True


# Alias for clarity in new code – Question == ContentItem
Question = ContentItem


# -------------------------------------------------------------------
# Lesson: what powers each card in the dashboard (/curriculum)
# -------------------------------------------------------------------
class Lesson(BaseModel):
    """
    Aggregated view of a lesson/collection.

    - Static metadata (grade, subject, topic, title, subtitle, icon, tags…)
    - Dynamic progress stats (attempted, correct, completion_pct…)
    """

    id: str                            # matches collection_id, e.g. "kg_math_numbers_1to5"
    grade: str                         # "KG", "Class-1"
    subject: str                       # "Math", "English"
    topic: str                         # "numbers", "addition", etc.
    subtopic: Optional[str] = None

    title: str                         # card title
    subtitle: str                      # one-line description
    icon: Optional[str] = None         # e.g. "📘"
    tags: List[str] = Field(default_factory=list)

    # For the card meta line / session estimates
    estimated_minutes: int = 5
    estimated_questions: Optional[int] = None    # override if needed

    # Optional hero art used by the UI when present
    image_url: Optional[str] = None

    # Progress/status for the current profile
    status: Literal["new", "in_progress", "done"] = "new"
    attempted: int = 0
    correct: int = 0
    completion_pct: float = 0.0        # 0–100, precomputed server-side


# -------------------------------------------------------------------
# QuestionSet: bundle of questions for one lesson / micro-skill
# -------------------------------------------------------------------
class QuestionSet(BaseModel):
    """
    Logical bundle of questions under one lesson/collection.
    Example: all questions for "kg_math_numbers_1to5".
    """

    id: str                            # e.g. "kg_math_numbers_1to5_v1"
    lesson_id: str                     # should match Lesson.id / collection_id
    grade: str
    subject: str
    topic: str
    subtopic: Optional[str] = None

    questions: List[Question]
    version: int = 1
    active: bool = True

    tags: List[str] = Field(default_factory=list)


# -------------------------------------------------------------------
# Tool payloads (for type hints & logs)
# -------------------------------------------------------------------
class ToolGetQuestion(BaseModel):
    lesson_id: Optional[str] = None
    last_question_id: Optional[str] = None


class ToolUpdateAnswer(BaseModel):
    lesson_id: Optional[str] = None
    question_id: str
    user_answer: str                  # model's transcript/normalized answer
    is_correct: bool                  # model's judgment
    transcript: Optional[str] = None  # optional raw transcript if you want it


# -------------------------------------------------------------------
# Events / profiling
# -------------------------------------------------------------------
class EventRecord(BaseModel):
    profile_id: str
    session_id: Optional[str] = None
    type: str  # e.g. "live_tool_get_question", "live_tool_update_answer", "hint_requested", etc.
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
