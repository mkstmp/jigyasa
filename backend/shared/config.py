import os

GOOGLE_API_KEY   = os.getenv("GOOGLE_API_KEY", "")
GEMINI_LIVE_MODEL = os.getenv("GEMINI_LIVE_MODEL", "gemini-2.5-flash-native-audio-preview-09-2025")
LIVE_INPUT_MIME  = "audio/pcm"
LIVE_OUTPUT_MIME = "audio/pcm"
CORS_ORIGINS = ["http://localhost:5173", "http://127.0.0.1:5173",
                "http://localhost:3000", "http://127.0.0.1:3000",
                "http://localhost:5500", "http://127.0.0.1:5500"]
