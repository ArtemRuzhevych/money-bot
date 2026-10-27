"""Claude Code on the server: install the official binary and run `claude -p` on the subscription."""

import hashlib
import json
import logging
import os
import platform
import re
import subprocess
import time
import urllib.request
from pathlib import Path

BASE = "https://downloads.claude.ai/claude-code-releases"
log = logging.getLogger(__name__)


def bin_path() -> Path:
    # Outside app/, so code updates don't delete the ~200 MB binary.
    return Path(os.environ.get("CLAUDE_BIN", Path.home() / ".money-bot" / "claude"))


def _fetch(url: str):
    return urllib.request.urlopen(url, timeout=120)


def platform_id(system: str = "", machine: str = "", libc: str | None = None) -> str:
    """Release platform name, same rules as the official install.sh."""
    system = (system or platform.system()).lower()
    machine = (machine or platform.machine()).lower()
    arch = {"x86_64": "x64", "amd64": "x64", "aarch64": "arm64", "arm64": "arm64"}.get(machine)
    if system != "linux" or arch is None:
        raise RuntimeError(f"unsupported platform {system}/{machine}")
    libc = platform.libc_ver()[0] if libc is None else libc
    return f"linux-{arch}" if libc == "glibc" else f"linux-{arch}-musl"


def install() -> str:
    """Install the latest release unless it is already there. Returns the version."""
    dest = bin_path()
    version = _fetch(f"{BASE}/latest").read().decode().strip()
    if not re.fullmatch(r"\d+\.\d+\.\d+\S*", version):
        raise RuntimeError(f"unexpected version response {version[:50]!r}")
    marker = dest.with_suffix(".version")
    if dest.exists() and marker.exists() and marker.read_text() == version:
        log.info("claude %s already installed", version)
        return version

    plat = platform_id()
    expected = json.load(_fetch(f"{BASE}/{version}/manifest.json"))["platforms"][plat]["checksum"]
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(".part")
    digest = hashlib.sha256()
    log.info("downloading claude %s for %s", version, plat)
    with _fetch(f"{BASE}/{version}/{plat}/claude") as src, part.open("wb") as out:
        while chunk := src.read(1 << 20):
            digest.update(chunk)
            out.write(chunk)
    if digest.hexdigest() != expected:
        part.unlink()
        raise RuntimeError(f"checksum mismatch for claude {version}")
    part.chmod(0o755)
    part.replace(dest)
    marker.write_text(version)
    log.info("installed claude %s (%d MB)", version, dest.stat().st_size >> 20)
    return version


def _peak_child_mb() -> int | None:
    try:
        import resource  # Unix only
    except ImportError:
        return None
    return resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss // 1024  # Linux reports KB


def ask(
    prompt: str, cwd: Path, model: str = "haiku", timeout: int = 120, cmd: list[str] | None = None
) -> dict:
    """One `claude -p` call with only the Read tool. Returns the answer, wall time and peak memory."""
    args = [*(cmd or [str(bin_path())]), "-p", prompt, "--output-format", "json", "--model", model]
    args += ["--allowedTools", "Read"]
    start = time.monotonic()
    proc = subprocess.run(
        args,
        cwd=cwd,
        env={**os.environ, "DISABLE_AUTOUPDATER": "1"},
        stdin=subprocess.DEVNULL,  # otherwise claude waits 3 s for piped input
        capture_output=True,
        text=True,
        timeout=timeout,  # raises TimeoutExpired and kills claude instead of hanging the bot
    )
    seconds = round(time.monotonic() - start, 1)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()[-500:]
        raise RuntimeError(f"claude exited {proc.returncode}: {detail}")
    out = json.loads(proc.stdout)
    if out.get("is_error"):
        raise RuntimeError(f"claude error: {out.get('result')}")
    return {"result": out.get("result", ""), "seconds": seconds, "peak_mb": _peak_child_mb()}
