# config.py
STATE = "South Carolina"  # CHANGE THIS TO TARGET STATE
MAX_CONCURRENT = 20
CACHE_DIR = "./cache"
OUTPUT_CSV = "boza_members.csv"

# Phase 5 uses Google Gemini through its OpenAI-compatible endpoint, so the
# existing `openai` client is reused (no extra packages). Set GEMINI_API_KEY in
# the environment. Falls back to OpenAI (OPENAI_API_KEY) if no Gemini key is set.
OPENAI_MODEL = "gemini-3.6-flash"
GEMINI_MODEL = "gemini-3.6-flash"
GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"
