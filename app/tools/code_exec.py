"""Small defense-in-depth Python subprocess sandbox for the MVP."""

from __future__ import annotations

import asyncio
import os
import re
import sys
import tempfile
from pathlib import Path

from ..config import CODE_TIMEOUT_SECONDS


_DENY_RE = re.compile(
    r"(?:\b(?:subprocess|socket|ctypes|multiprocessing|shutil|pathlib)\b|"
    r"\b(?:eval|exec|compile|__import__|open|input)\s*\(|"
    r"\bimport\s+(?:os|sys|subprocess|socket)|"
    r"\bfrom\s+(?:os|sys|subprocess|socket)\b|"
    r"rm\s+-rf|/etc/|\.\./|https?://)",
    re.I,
)


def _trim(value: bytes, limit: int = 12_000) -> str:
    text = value.decode("utf-8", errors="replace")
    return text if len(text) <= limit else text[:limit] + "\n… output truncated …"


async def run_python(code: str) -> dict:
    if not code.strip():
        return {"ok": False, "stdout": "", "stderr": "Rejected: empty program"}
    match = _DENY_RE.search(code)
    if match:
        return {"ok": False, "stdout": "", "stderr": f"Rejected by sandbox policy: {match.group(0)!r}"}

    with tempfile.TemporaryDirectory(prefix="atlas-sandbox-") as directory:
        script = Path(directory) / "main.py"
        script.write_text(code, encoding="utf-8")
        clean_env = {
            "PATH": os.defpath,
            "PYTHONIOENCODING": "utf-8",
            "PYTHONHASHSEED": "0",
        }
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-I",
            str(script),
            cwd=directory,
            env=clean_env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=CODE_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            process.kill()
            stdout, stderr = await process.communicate()
            return {"ok": False, "stdout": _trim(stdout), "stderr": f"Timed out after {CODE_TIMEOUT_SECONDS}s\n{_trim(stderr)}"}
        return {"ok": process.returncode == 0, "stdout": _trim(stdout), "stderr": _trim(stderr)}
