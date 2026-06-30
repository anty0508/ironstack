import os
import sys
from pathlib import Path

from dotenv import load_dotenv


def _base_dir() -> Path:
    """Directory the app runs from. When frozen to an .exe this is the folder
    containing the executable; in development it's the project root."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).parent


def resource_path(*parts) -> Path:
    """Locate a bundled read-only resource (e.g. assets/IronStack.ico). Works
    both from source and inside the PyInstaller one-file bundle, where data is
    extracted to sys._MEIPASS rather than sitting next to the executable."""
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    else:
        base = Path(__file__).resolve().parent   # project root (config.py lives here)
    return base.joinpath(*parts)


# --- Storage ---
BASE_DIR = _base_dir()

# API keys come from a .env, resolved in priority order (load_dotenv never
# overrides an already-set var, so the first hit wins):
#   1. a .env sitting next to the .exe   -> lets a user drop in their own keys
#   2. the usual search from the CWD      -> running from source in development
#   3. a .env baked into the build        -> makes IronStack.exe self-contained
# Environment variables set on the machine still take precedence over all three.
load_dotenv(BASE_DIR / ".env")
load_dotenv()
load_dotenv(resource_path(".env"))
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "interview.db"
LOG_PATH = DATA_DIR / "ironstack.log"

# --- OpenAI (answer generation: text -> short answer) ---
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
ANSWER_MODEL = "gpt-5-nano"

# --- Deepgram (speech-to-text: system audio -> text, real-time) ---
DEEPGRAM_API_KEY = os.getenv("DEEPGRAM_API_KEY", "")
DEEPGRAM_URL = "wss://api.deepgram.com/v1/listen"
DEEPGRAM_MODEL = "nova-3"      # newest general streaming model
DEEPGRAM_LANGUAGE = "en"       # default; overridden per meeting (see LANGUAGES)

# --- Languages (transcription + AI answer language, picked together) ---
# (display label, Deepgram streaming language code). All are supported by the
# nova-3 streaming model. The label doubles as the language name handed to the
# answer model ("respond in <label>").
LANGUAGES = [
    ("English", "en"),
    ("Spanish", "es"),
    ("French", "fr"),
    ("German", "de"),
    ("Japanese", "ja"),
    ("Chinese", "zh"),
    ("Hindi", "hi"),
    ("Portuguese", "pt"),
]
DEFAULT_LANGUAGE_CODE = "en"


def language_name(code):
    """Human label for a Deepgram language code (falls back to the code)."""
    for label, c in LANGUAGES:
        if c == code:
            return label
    return code

# --- Audio capture ---
CAPTURE_SR = 48000      # sample rate read from the audio device
TARGET_SR = 24000       # sample rate sent to Deepgram
BLOCK_FRAMES = 1024     # frames per capture block (smaller = lower latency)

# --- Endpointing (when a spoken turn is considered finished) ---
# Deepgram emits word-by-word interim results continuously; an utterance is
# finalized after a short pause in speech.
ENDPOINTING_MS = 300      # silence (ms) before a chunk is marked speech_final
UTTERANCE_END_MS = 1000   # silence (ms) before an UtteranceEnd is emitted (backstop)

# --- Behaviour ---
# Transcript fragments shorter than this are buffered and merged into the
# next one, so short pieces like "yeah" or "okay" don't trigger an answer.
MIN_TEXT_LENGTH = 10
