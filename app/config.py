"""Environment and filesystem configuration for Atlas."""

from __future__ import annotations

import os
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parent.parent


def _load_env(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def _bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(int(default))).strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


_load_env(ROOT_DIR / ".env")

FORCE_DEMO = _bool("FORCE_DEMO", True)
APPROVAL_THRESHOLD = _int("APPROVAL_THRESHOLD", 60)
MAX_STEP_RETRIES = _int("MAX_STEP_RETRIES", 2)
CODE_TIMEOUT_SECONDS = _int("CODE_TIMEOUT_SECONDS", 8)
APPROVAL_TIMEOUT_SECONDS = _int("APPROVAL_TIMEOUT_SECONDS", 600)

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
CEREBRAS_API_KEY = os.getenv("CEREBRAS_API_KEY", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")

ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-3-5-haiku-latest")
CEREBRAS_MODEL = os.getenv("CEREBRAS_MODEL", "llama-3.3-70b")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.2")

DATA_DIR = Path(os.getenv("ATLAS_DATA_DIR", str(ROOT_DIR / "data"))).expanduser().resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)
AUDIT_LOG = DATA_DIR / "audit.jsonl"
MEMORY_FILE = DATA_DIR / "memory.json"
