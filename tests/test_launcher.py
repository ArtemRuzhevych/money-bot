import io
import logging
import os
import tarfile

import pytest

import launcher


def make_tarball(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, text in files.items():
            data = text.encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def test_install_strips_github_root_and_writes_sha(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "stale.py").write_text("old")

    launcher.install("abc123", make_tarball({"owner-repo-abc123/money_bot/__main__.py": "print(1)"}))

    assert (tmp_path / "app" / "money_bot" / "__main__.py").read_text() == "print(1)"
    assert not (tmp_path / "app" / "stale.py").exists()
    assert (tmp_path / "app" / ".sha").read_text() == "abc123"
    assert not (tmp_path / "app.new").exists()


def test_install_rejects_path_traversal(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(tarfile.FilterError):
        launcher.install("x", make_tarball({"root/../../evil.py": "boom"}))
    assert not (tmp_path / "evil.py").exists()  # app.new/root/../../ is tmp_path


def test_setup_logging_writes_file_and_exports_log_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))

    launcher.setup_logging("unit")
    launcher.log.info("hello from test")
    for handler in logging.root.handlers[:]:  # close the file so other tests don't log into it
        handler.close()
        logging.root.removeHandler(handler)

    assert "INFO launcher: hello from test" in (tmp_path / "logs" / "unit.log").read_text()


def test_load_env_keeps_real_env_and_strips_quotes(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text('# comment\nMB_A="quoted"\nMB_B=from_file\n\nnot a pair\n')
    monkeypatch.setenv("MB_B", "from_env")
    monkeypatch.delenv("MB_A", raising=False)

    launcher.load_env(env)

    assert os.environ["MB_A"] == "quoted"
    assert os.environ["MB_B"] == "from_env"


@pytest.mark.parametrize(("token", "auth"), [(None, None), ("tok", "Bearer tok")])
def test_github_token_is_optional(monkeypatch, token, auth):
    sent = []
    monkeypatch.setattr(launcher.urllib.request, "urlopen", lambda req, timeout: sent.append(req))
    monkeypatch.setenv("GITHUB_REPO", "someone/money-bot")
    if token:
        monkeypatch.setenv("GITHUB_TOKEN", token)
    else:
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    launcher.gh("/commits/deploy")

    assert sent[0].full_url == "https://api.github.com/repos/someone/money-bot/commits/deploy"
    assert sent[0].get_header("Authorization") == auth
