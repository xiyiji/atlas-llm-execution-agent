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


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


_load_env(ROOT_DIR / ".env")

FORCE_DEMO = _bool("FORCE_DEMO", True)
LLM_FALLBACK_TO_DEMO = _bool("LLM_FALLBACK_TO_DEMO", False)
APPROVAL_THRESHOLD = _int("APPROVAL_THRESHOLD", 60)
MAX_STEP_RETRIES = _int("MAX_STEP_RETRIES", 2)
CODE_TIMEOUT_SECONDS = _int("CODE_TIMEOUT_SECONDS", 8)
APPROVAL_TIMEOUT_SECONDS = _int("APPROVAL_TIMEOUT_SECONDS", 600)
MAX_REQUEST_BYTES = _int("MAX_REQUEST_BYTES", 1_000_000)
RATE_LIMIT_PER_MINUTE = _int("RATE_LIMIT_PER_MINUTE", 120)
WEB_MAX_RESPONSE_BYTES = _int("WEB_MAX_RESPONSE_BYTES", 2_000_000)
SANDBOX_MEMORY_MB = _int("SANDBOX_MEMORY_MB", 128)
SANDBOX_CPUS = _float("SANDBOX_CPUS", 0.5)
SANDBOX_PIDS_LIMIT = _int("SANDBOX_PIDS_LIMIT", 64)

ENVIRONMENT = os.getenv("ATLAS_ENV", "development").strip().lower()
AUTH_REQUIRED = _bool("AUTH_REQUIRED", not FORCE_DEMO)
AUDIT_FILE_ENABLED = _bool("AUDIT_FILE_ENABLED", True)
MEMORY_FILE_ENABLED = _bool("MEMORY_FILE_ENABLED", True)
API_KEYS = os.getenv("ATLAS_API_KEYS", "")
SESSION_SECRET = os.getenv("SESSION_SECRET", "dev-only-change-me")
SESSION_TTL_SECONDS = _int("SESSION_TTL_SECONDS", 3600)
EXECUTION_BACKEND = os.getenv("EXECUTION_BACKEND", "inprocess").strip().lower()
SANDBOX_BACKEND = os.getenv("SANDBOX_BACKEND", "local" if FORCE_DEMO else "docker").strip().lower()
REDIS_URL = os.getenv("REDIS_URL", "").strip()
CELERY_BROKER_URL = os.getenv("CELERY_BROKER_URL", REDIS_URL or "redis://localhost:6379/0")
CELERY_RESULT_BACKEND = os.getenv("CELERY_RESULT_BACKEND", REDIS_URL or "redis://localhost:6379/1")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
TRUSTED_HOSTS = [item.strip() for item in os.getenv("TRUSTED_HOSTS", "localhost,127.0.0.1,testserver").split(",") if item.strip()]
ALLOWED_ORIGINS = [item.strip() for item in os.getenv("ALLOWED_ORIGINS", "").split(",") if item.strip()]

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
DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{DATA_DIR / 'atlas.db'}")


def validate_production() -> None:
    if ENVIRONMENT != "production":
        return
    problems = []
    if not AUTH_REQUIRED:
        problems.append("AUTH_REQUIRED must be enabled")
    if not API_KEYS:
        problems.append("ATLAS_API_KEYS must contain at least one tenant:key pair")
    if SESSION_SECRET == "dev-only-change-me" or len(SESSION_SECRET) < 32:
        problems.append("SESSION_SECRET must be at least 32 characters")
    if EXECUTION_BACKEND not in {"inprocess", "celery"}:
        problems.append("EXECUTION_BACKEND must be inprocess or celery")
    if SANDBOX_BACKEND not in {"docker"}:
        problems.append("SANDBOX_BACKEND must be docker in production")
    if problems:
        raise RuntimeError("Invalid production configuration: " + "; ".join(problems))
