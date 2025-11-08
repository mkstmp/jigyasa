# Jigyāsa

Kid-friendly, voice-first prototype using Google Gemini Live (audio-in / audio-out).
Frontend: plain HTML + Web Audio. Backend: FastAPI + google-genai Live API.

---

## Quick Start

### Install

    python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
    pip install fastapi uvicorn "google-genai>=0.3.0"

### 1) Backend

    uvicorn backend.main:app --reload --host 0.0.0.0 --port 8000

### 2) Frontend (static)

    # from project root
    python3 -m http.server 5173 -d frontend

Open http://localhost:5173

---

## Environment

Set these in your shell (or load from a .env in your config):

- GOOGLE_API_KEY — your Google AI Studio key (bearer)
- GEMINI_LIVE_MODEL — e.g. gemini-2.5-flash-native-audio-preview-09-2025

Optional:
- CORS_ORIGINS — JSON array of allowed origins
  Defaults to ["http://localhost:5173","http://127.0.0.1:5173"]

Note: You do not need an SDP URL; the app uses the official google-genai SDK’s live.connect().

---

## How it works

- Browser captures mic audio (48 kHz), downsamples to 16 kHz PCM16 mono with a small FIR low-pass filter inside an AudioWorklet, and streams to /live/ws.
- Backend forwards audio to Gemini Live and streams 24 kHz PCM16 mono back.
- Frontend plays model audio smoothly via a jitter-tolerant scheduler and adapts to the server rate via a "pcm_meta" message.

---

## Project Structure (essential)

    backend/
      main.py                # FastAPI app + /live/ws (PCM bridge)
      live/gemini_live.py    # Gemini Live bridge, tool-calling, logging
      agents/tool_router.py  # Tool logic: get_question, update_answer
      content/library.py     # Loads content.json
      content/content.json   # Your questions (lean schema)
      shared/schemas.py      # Pydantic models (lean)
    frontend/
      index.html             # Single-page client (mic + playback)

---

## Content schema (lean)

Example content.json:

    {
      "items": [
        {
          "id": "mult_4x4",
          "topic": "numbers",
          "subtopic": "multiplication",
          "difficulty": "easy",
          "question_text": "What is four times four?",
          "type": "mcq",
          "choices": ["16", "18", "15", "12"],
          "answer": "16",
          "hints": ["Skip count by 4.", "Think of 4 groups of 4."],
          "tags": ["table:4", "factor:4", "k-2"]
        },
        {
          "id": "shape_circle_01",
          "topic": "shapes",
          "subtopic": "identify",
          "difficulty": "easy",
          "question_text": "Look at the picture. What shape is this?",
          "type": "open",
          "answer": "circle",
          "hints": ["It’s round and has no corners."],
          "media": { "image": { "url": "/static/img/circle.svg", "alt": "a round circle" } }
        },
        {
          "id": "flowers_list_5",
          "topic": "vocab",
          "subtopic": "flowers",
          "difficulty": "easy",
          "question_text": "Tell me five flower names.",
          "type": "list",
          "list_spec": { "required": 5, "category": "flowers" },
          "hints": ["Think of flowers you see in gardens."]
        }
      ]
    }

Notes:
- question_text replaces old prompt_visual/prompt_tts.
- type ∈ "mcq" | "open" | "list".
- list_spec only for "list" items.
- answer optional for "open"/"mcq", unused for "list".
- choices only for "mcq".

---

## Troubleshooting

- No mic audio? Ensure the tab has mic permission (macOS: System Settings → Privacy & Security → Microphone).
- Echo / “double voice”? echoCancellation is enabled; use headphones for best results.
- Choppy playback? Keep the frontend on localhost and avoid background-throttling the tab. The playback scheduler smooths typical jitter.
- CORS errors? Make sure your frontend runs on port 5173 or add your origin to CORS_ORIGINS.

---

## Suggested .gitignore

    .venv/
    __pycache__/
    *.pyc
    *.log
    .env

---

## Credits

- Google google-genai Live API
- Web Audio (AudioWorklet for high-quality 48k→16k downsample + smooth scheduler)
- Tool calling: get_question and update_answer routed in backend for clarity and logging.

