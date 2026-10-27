import hashlib
import io
import json
import subprocess
import sys

import pytest

from money_bot import claude

BINARY = b"\x7fELF fake claude"


@pytest.mark.parametrize(
    ("machine", "libc", "expected"),
    [
        ("x86_64", "glibc", "linux-x64"),
        ("aarch64", "glibc", "linux-arm64"),
        ("x86_64", "", "linux-x64-musl"),
    ],
)
def test_platform_id(machine, libc, expected):
    assert claude.platform_id("Linux", machine, libc) == expected


def test_platform_id_rejects_unknown_arch():
    with pytest.raises(RuntimeError):
        claude.platform_id("Linux", "riscv64", "glibc")


def fake_release(monkeypatch, tmp_path, checksum):
    monkeypatch.setenv("CLAUDE_BIN", str(tmp_path / "claude"))
    monkeypatch.setattr(claude, "platform_id", lambda: "linux-x64")
    manifest = json.dumps({"platforms": {"linux-x64": {"checksum": checksum}}}).encode()
    downloads = []

    def fetch(url):
        downloads.append(url)
        if url.endswith("/latest"):
            return io.BytesIO(b"2.1.0\n")
        if url.endswith("/manifest.json"):
            return io.BytesIO(manifest)
        return io.BytesIO(BINARY)

    monkeypatch.setattr(claude, "_fetch", fetch)
    return downloads


def test_install_verifies_and_skips_when_current(monkeypatch, tmp_path):
    downloads = fake_release(monkeypatch, tmp_path, hashlib.sha256(BINARY).hexdigest())

    assert claude.install() == "2.1.0"
    assert (tmp_path / "claude").read_bytes() == BINARY
    assert (tmp_path / "claude.version").read_text() == "2.1.0"

    downloads.clear()
    claude.install()
    assert downloads == [f"{claude.BASE}/latest"]  # only the version check, no re-download


def test_install_rejects_bad_checksum(monkeypatch, tmp_path):
    fake_release(monkeypatch, tmp_path, "0" * 64)

    with pytest.raises(RuntimeError, match="checksum"):
        claude.install()
    assert not (tmp_path / "claude").exists()
    assert not (tmp_path / "claude.part").exists()


def fake_claude(tmp_path, body: str) -> list[str]:
    script = tmp_path / "fake_claude.py"
    script.write_text(f"import json, sys, time\nargs = sys.argv[1:]\n{body}\n")
    return [sys.executable, str(script)]


def test_ask_passes_prompt_and_only_allows_read(tmp_path):
    cmd = fake_claude(tmp_path, "print(json.dumps({'result': ' '.join(args)}))")

    out = claude.ask("hello", cwd=tmp_path, model="sonnet", cmd=cmd)

    assert out["result"].startswith("-p hello --output-format json --model sonnet")
    assert out["result"].endswith("--allowedTools Read")
    assert out["seconds"] >= 0


@pytest.mark.parametrize(
    "body",
    [
        "sys.stderr.write('auth failed'); sys.exit(1)",
        "print(json.dumps({'is_error': True, 'result': 'rate limited'}))",
    ],
)
def test_ask_raises_on_failure(tmp_path, body):
    with pytest.raises(RuntimeError):
        claude.ask("x", cwd=tmp_path, cmd=fake_claude(tmp_path, body))


def test_ask_times_out(tmp_path):
    with pytest.raises(subprocess.TimeoutExpired):
        claude.ask("x", cwd=tmp_path, timeout=1, cmd=fake_claude(tmp_path, "time.sleep(10)"))
