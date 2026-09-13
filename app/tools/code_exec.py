"""Defense-in-depth Python execution sandbox."""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import sys
import tempfile
import uuid
from pathlib import Path

from .. import config

CODE_TIMEOUT_SECONDS = config.CODE_TIMEOUT_SECONDS


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


async def _communicate(process: asyncio.subprocess.Process, stdin: bytes | None = None, on_timeout=None) -> dict:
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(stdin), timeout=CODE_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        if on_timeout is not None:
            await on_timeout()
        if process.returncode is None:
            process.kill()
        stdout, stderr = await process.communicate()
        return {"ok": False, "stdout": _trim(stdout), "stderr": f"Timed out after {CODE_TIMEOUT_SECONDS}s\n{_trim(stderr)}"}
    return {"ok": process.returncode == 0, "stdout": _trim(stdout), "stderr": _trim(stderr)}


async def _run_local(script: Path, directory: str) -> dict:
    if config.ENVIRONMENT == "production":
        return {"ok": False, "stdout": "", "stderr": "Local sandbox is disabled in production"}
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-I",
        str(script),
        cwd=directory,
        env={"PATH": os.defpath, "PYTHONIOENCODING": "utf-8", "PYTHONHASHSEED": "0"},
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    return await _communicate(process)


async def _run_docker(script: Path) -> dict:
    docker = shutil.which("docker")
    if not docker:
        return {"ok": False, "stdout": "", "stderr": "Docker sandbox requested but Docker is unavailable"}
    source = await asyncio.to_thread(script.read_bytes)
    container_name = f"atlas-sandbox-{uuid.uuid4().hex}"
    process = await asyncio.create_subprocess_exec(
        docker,
        "run",
        "--rm",
        "--name",
        container_name,
        "--interactive",
        "--network=none",
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        f"--memory={config.SANDBOX_MEMORY_MB}m",
        f"--cpus={config.SANDBOX_CPUS}",
        f"--pids-limit={config.SANDBOX_PIDS_LIMIT}",
        "--tmpfs=/tmp:rw,noexec,nosuid,size=16m",
        "python:3.12-alpine",
        "python",
        "-I",
        "-",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={"PATH": os.environ.get("PATH", os.defpath)},
    )
    async def force_remove() -> None:
        cleanup = await asyncio.create_subprocess_exec(
            docker,
            "rm",
            "--force",
            container_name,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            env={"PATH": os.environ.get("PATH", os.defpath)},
        )
        await cleanup.communicate()

    return await _communicate(process, source, force_remove)


async def run_python(code: str) -> dict:
    if not code.strip():
        return {"ok": False, "stdout": "", "stderr": "Rejected: empty program"}
    match = _DENY_RE.search(code)
    if match:
        return {"ok": False, "stdout": "", "stderr": f"Rejected by sandbox policy: {match.group(0)!r}"}

    with tempfile.TemporaryDirectory(prefix="atlas-sandbox-") as directory:
        script = Path(directory) / "main.py"
        script.write_text(code, encoding="utf-8")
        if config.SANDBOX_BACKEND == "docker":
            return await _run_docker(script)
        return await _run_local(script, directory)
