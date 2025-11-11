# backend/content/library.py
import json, os, random
from typing import List, Optional, Dict, Any

# Load content.json at import
DATA_PATH = os.path.join(os.path.dirname(__file__), "content_1_math.json")


def _normalize_item(it: Dict[str, Any]) -> Dict[str, Any]:
    """Ensure forward-compatible fields exist and avoid brittle casing."""
    it = dict(it)  # shallow copy

    # Ensure question_text exists (prefer new field; fallback to legacy fields)
    if not it.get("question_text"):
        it["question_text"] = it.get("prompt_tts") or it.get("prompt_visual") or ""

    # Optional: default missing structural fields to keep downstream code simple
    it.setdefault("type", "open")            # if missing, assume "open"
    it.setdefault("topic", "")
    it.setdefault("difficulty", "")
    it.setdefault("subtopic", None)
    it.setdefault("choices", [])
    it.setdefault("answer", None)
    # list_spec is passed through if present

    return it


def _load_items() -> List[dict]:
    with open(DATA_PATH, "r") as f:
        data = json.load(f)
    # Accept either a top-level list OR {"items":[...]}
    items = data["items"] if isinstance(data, dict) and "items" in data else data
    return [_normalize_item(x) for x in items]


ITEMS: List[dict] = _load_items()


def _eq_ci(a: Optional[str], b: Optional[str]) -> bool:
    """Case-insensitive equality (treat None/'' as equal only if both falsy)."""
    if not a and not b:
        return True
    if a is None or b is None:
        return False
    return a.lower() == b.lower()


def _filter_candidates(topic: Optional[str], difficulty: Optional[str], subtopic: Optional[str]) -> List[dict]:
    cands = list(ITEMS)
    if topic:
        cands = [c for c in cands if _eq_ci(c.get("topic"), topic)]
    if difficulty:
        cands = [c for c in cands if _eq_ci(c.get("difficulty"), difficulty)]
    if subtopic:
        cands = [c for c in cands if _eq_ci(c.get("subtopic"), subtopic)]
    return cands


def fetch(
    topic: Optional[str],
    difficulty: Optional[str],
    subtopic: Optional[str],
    exclude: Optional[List[str]] = None
) -> Optional[dict]:
    """Legacy fetch used by old endpoints; keep behavior but make it case-insensitive."""
    exclude_set = set(exclude or [])

    # First pass: exact filters
    cands = [c for c in _filter_candidates(topic, difficulty, subtopic) if c.get("id") not in exclude_set]
    if cands:
        return random.choice(cands)

    # Relaxed: match topic only
    if topic:
        cands = [c for c in ITEMS if _eq_ci(c.get("topic"), topic) and c.get("id") not in exclude_set]
        if cands:
            return random.choice(cands)

    # Last resort: ANY item not excluded
    cands = [c for c in ITEMS if c.get("id") not in exclude_set]
    if cands:
        return random.choice(cands)

    return None


def get_by_id(item_id: str) -> Optional[dict]:
    """Convenience helper for debug endpoints."""
    for it in ITEMS:
        if it.get("id") == item_id:
            return it
    return None
