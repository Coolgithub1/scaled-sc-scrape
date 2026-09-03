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

# Phase 4 historic minutes archives (year-by-year crawl, present → oldest available).
# No fixed start year: CivicPlus/Drupal year lists on each county site decide how
# far back we go. ARCHIVE_YEAR_FLOOR is only a safety bound if a portal lists nothing.
# MAX_DOCS_PER_YEAR: minutes kept per archive year (prefer minutes over agendas).
# MAX_DOCS_KEEP: hard cap on documents fed to the LLM per county.
# MAX_DOCS_SCAN: max document URLs to download/sniff per county.
ARCHIVE_YEAR_FLOOR = 1990
MAX_DOCS_PER_YEAR = 2
MAX_DOCS_KEEP = 20
MAX_DOCS_SCAN = 80
