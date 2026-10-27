"""Launcher: keeps the bot running on the latest commit of the deploy branch.

Made for Pterodactyl-style Python hosts with no shell (tested on Daki). Upload this file once to
the server root and set it as the startup file.
Secrets live in .env next to it (outside app/, so updates never touch them).
"""

import io
import json
import logging
import os
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.request
from logging.handlers import RotatingFileHandler
from pathlib import Path

APP = Path("app")
SHA_FILE = APP / ".sha"
TICK = 10  # seconds between crash checks

log = logging.getLogger("launcher")


def setup_logging(name: str) -> None:
    """Log to the console and logs/<name>.log; the host's console history is lost on restart.

    The bot imports this too (launcher.py ships inside app/), so both log the same way.
    """
    log_dir = Path(os.environ.setdefault("LOG_DIR", str(Path("logs").resolve())))
    log_dir.mkdir(exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")
    handlers = [
        logging.StreamHandler(sys.stdout),
        RotatingFileHandler(log_dir / f"{name}.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8"),
    ]
    for handler in handlers:
        handler.setFormatter(fmt)
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), handlers=handlers, force=True)


def load_env(path: Path) -> None:
    """Read KEY=VALUE lines into os.environ; real env vars win."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def gh(path: str):
    headers = {"Accept": "application/vnd.github+json"}
    if os.environ.get("GITHUB_TOKEN"):  # needed for a private repo; a public one works without
        headers["Authorization"] = f"Bearer {os.environ['GITHUB_TOKEN']}"
    url = f"https://api.github.com/repos/{os.environ['GITHUB_REPO']}{path}"
    req = urllib.request.Request(url, headers=headers)
    return urllib.request.urlopen(req, timeout=60)


def latest_sha() -> str:
    branch = os.environ.get("DEPLOY_BRANCH", "deploy")
    return json.load(gh(f"/commits/{branch}"))["sha"]


def install(sha: str, tarball: bytes) -> None:
    """Unpack a GitHub tarball into app/, install deps, then swap it in."""
    new = Path("app.new")
    shutil.rmtree(new, ignore_errors=True)
    with tarfile.open(fileobj=io.BytesIO(tarball)) as tar:
        tar.extractall(new, filter="data")  # filter blocks path traversal
    (root,) = new.iterdir()  # GitHub wraps the repo in one top-level folder
    reqs = root / "requirements.txt"
    if reqs.exists():
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", "-r", str(reqs)], check=True)
    shutil.rmtree(APP, ignore_errors=True)
    root.rename(APP)
    shutil.rmtree(new)
    SHA_FILE.write_text(sha)


def stop(child: subprocess.Popen | None) -> None:
    if child and child.poll() is None:
        child.terminate()
        try:
            child.wait(30)
        except subprocess.TimeoutExpired:
            child.kill()


def main() -> None:
    load_env(Path(".env"))
    setup_logging("launcher")
    poll = int(os.environ.get("POLL_SECONDS", "300"))
    current = SHA_FILE.read_text().strip() if SHA_FILE.exists() else ""
    child = None
    next_check = 0.0
    while True:
        if time.monotonic() >= next_check:
            next_check = time.monotonic() + poll
            try:
                sha = latest_sha()
                if sha != current:
                    log.info("updating %s -> %s", current[:7] or "nothing", sha[:7])
                    install(sha, gh(f"/tarball/{sha}").read())
                    current = sha
                    stop(child)
                    child = None
            except Exception:  # keep the running bot alive if GitHub or pip fails
                log.exception("update failed")
        if current and (child is None or child.poll() is not None):
            if child is not None:
                log.warning("bot exited with %s, restarting", child.returncode)
            log.info("starting bot %s", current[:7])
            child = subprocess.Popen([sys.executable, "-m", "money_bot"], cwd=APP)
        time.sleep(TICK)


if __name__ == "__main__":
    main()
