"""Shared offline helpers for unit tests."""

from pathlib import Path

import pytest
import yaml

from helpers.github_transport import record_gh


@pytest.fixture(autouse=True)
def offline_acceptance(tmp_path, monkeypatch, isolated_home):
    """Keep temporary worktrees, HOME, terminals and all external I/O isolated."""
    import socket
    import tempfile

    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    monkeypatch.setenv("FORAGENT_ORCH_SPAWN", "off")

    def no_network(*args, **kwargs):
        raise AssertionError("acceptance tests must not access the network")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)
    calls = record_gh(monkeypatch)
    yield calls
    assert calls == []


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    for key, directory in (("XDG_CONFIG_HOME", ".config"), ("XDG_DATA_HOME", ".local/share"),
                           ("XDG_CACHE_HOME", ".cache")):
        monkeypatch.setenv(key, str(home / directory))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(home / ".gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "0")
    # Prevent default remote skill libraries from leaking into offline cases.
    _write_global_config({"skill_libraries": []})
    return home


def _write_global_config(values):
    path = Path.home() / ".config/foragent/config.yml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(values), encoding="utf-8")
    return path


def cli_exit(main, argv):
    """Treat argparse's nonzero SystemExit as the CLI's observable exit code."""
    try:
        return main(argv)
    except SystemExit as exc:
        return exc.code
