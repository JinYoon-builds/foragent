"""Acceptance test for claude-api-key. Locked by Foragent; do not edit.

Launch A2: API authentication is availability, never implicit spend consent.
"""

from datetime import UTC, datetime
import json
from pathlib import Path
import shutil
import subprocess

import pytest
import yaml

from test_init import setup
from test_init_role_agents import init_project
from test_skills_autosync import cli_exit, isolated_home


def test_api_key_init_accepts_logged_out_cli_but_does_not_authorize_spend(
    setup, init_project, monkeypatch, tmp_path, capsys,
):
    from foragent import cli
    from foragent.config import load_config
    from foragent.scheduler_quota import decide
    from foragent.state import StateStore

    monkeypatch.setenv("ANTHROPIC_API_KEY", "offline-test-key-not-a-credential")
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    original = subprocess.run
    original_which = shutil.which
    monkeypatch.setattr(shutil, "which", lambda name, *a, **kw:
                        "/offline/bin/claude" if name == "claude" else original_which(name, *a, **kw))

    def probe(argv, *args, **kwargs):
        command = [Path(argv[0]).name, *argv[1:]] if isinstance(argv, (list, tuple)) and argv else argv
        if command == ["codex", "login", "status"]:
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="Not logged in")
        if command == ["claude", "auth", "status"]:
            return subprocess.CompletedProcess(argv, 1, stdout=json.dumps({"loggedIn": False}), stderr="")
        if command == ["claude", "--version"]:
            return subprocess.CompletedProcess(argv, 0, stdout="2.0.0 (Claude Code)\n", stderr="")
        return original(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", probe)
    code = cli_exit(cli.main, ["init", "--repo-root", str(setup.root), "--no-push",
                               "--no-launchd", "--no-board", "--statusline", "no"])
    captured = capsys.readouterr()
    assert code == 0, f"Installed Claude Code with an API key is available: {captured.out}{captured.err}"
    config = load_config(setup.root)
    assert config.executor == "claude"
    saved = (setup.root / ".foragent.yml").read_text(encoding="utf-8")
    assert "offline-test-key" not in saved, "Never persist the API secret"
    assert "spend_cap" not in saved, "Init must not invent spending permission"
    assert "spend_cap" in captured.out + captured.err, "Init tells the person, in one line, how to allow capped spend"
    now = datetime(2026, 10, 29, tzinfo=UTC)
    store = StateStore(tmp_path / "api-state", now=lambda: now)
    start = decide(store, config, "main", now, 1)
    assert start.held or start.implementer != "claude", (
        "Uncapped API-key Claude must not implement: the start falls back or waits")


def test_api_spending_uses_786_explicit_cap_contract(tmp_path, monkeypatch):
    from foragent.config import ConfigError, load_config
    from foragent.scheduler_quota import decide
    from foragent.state import StateStore

    monkeypatch.setenv("ANTHROPIC_API_KEY", "offline-test-key-not-a-credential")
    path = tmp_path / ".foragent.yml"
    # #786's existing acceptance contract: executors=[{name, quota, spend_cap}].
    values = {"executor": "claude", "reviewer": "none", "skill_libraries": [],
              "executors": [{"name": "claude", "quota": "api", "spend_cap": 10.5}]}
    path.write_text(yaml.safe_dump(values), encoding="utf-8")
    try:
        config = load_config(tmp_path)
    except ConfigError as exc:
        pytest.fail(f"The explicit API spend_cap contract is not supported yet: {exc}")
    now = datetime(2026, 10, 29, tzinfo=UTC)
    store = StateStore(tmp_path / "state", now=lambda: now)
    assert not decide(store, config, "main", now, 1).held
    record = getattr(store, "record_spend", None)
    assert callable(record), "#786's public spend ledger is required to enforce the cap"
    record("claude", 10.5)
    assert decide(StateStore(store.root, now=lambda: now), config, "main", now, 2).held
    values["executors"][0].pop("spend_cap")
    path.write_text(yaml.safe_dump(values), encoding="utf-8")
    with pytest.raises(ConfigError, match="spend_cap"):
        load_config(tmp_path)


def test_without_an_api_key_in_the_gate_environment_claude_starts_as_before(tmp_path, monkeypatch):
    from foragent.config import load_config
    from foragent.scheduler_quota import decide
    from foragent.state import StateStore

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    (tmp_path / ".foragent.yml").write_text(yaml.safe_dump(
        {"executor": "claude", "reviewer": "none", "skill_libraries": []}), encoding="utf-8")
    config = load_config(tmp_path)
    now = datetime(2026, 10, 29, tzinfo=UTC)
    store = StateStore(tmp_path / "state", now=lambda: now)
    assert not decide(store, config, "main", now, 1).held, "A logged-in subscription setup is unchanged"
