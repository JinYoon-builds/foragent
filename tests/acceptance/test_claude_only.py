"""Acceptance test for claude-only. Locked by Foragent; do not edit."""

from pathlib import Path
import json
import shutil
import subprocess

from test_init import setup
from test_skills_autosync import cli_exit, isolated_home


def init_with_tool_facts(setup, monkeypatch, *, codex, claude):
    from foragent import cli

    calls = []
    original_run = subprocess.run
    original_which = shutil.which

    def which(command, *args, **kwargs):
        if command in {"codex", "claude"}:
            return f"/offline/bin/{command}"  # Installed does not mean authenticated.
        return original_which(command, *args, **kwargs)

    def probe(argv, *args, **kwargs):
        if isinstance(argv, (list, tuple)) and argv and Path(argv[0]).name in {"codex", "claude"}:
            tool, command = Path(argv[0]).name, list(argv[1:])
            calls.append((tool, command))
            assert command in (["login", "status"], ["auth", "status"], ["--version"]), (
                "Init must only probe tools, never invoke an LLM", argv)
            logged_in = codex if tool == "codex" else claude
            if command == ["--version"]:
                code, output = (0, "2.0.0 (Claude Code)\n") if logged_in else (1, "")
            elif tool == "claude":
                code, output = (0 if logged_in else 1), json.dumps({"loggedIn": logged_in, "authMethod": "claude.ai"})
            else:
                code, output = (0, "Logged in using ChatGPT\n") if logged_in else (1, "Not logged in\n")
            if code and kwargs.get("check"):
                raise subprocess.CalledProcessError(code, argv, output=output, stderr="Not logged in")
            return subprocess.CompletedProcess(argv, code, stdout=output, stderr="" if not code else "Not logged in")
        return original_run(argv, *args, **kwargs)

    monkeypatch.setattr(shutil, "which", which)
    monkeypatch.setattr(subprocess, "run", probe)
    # Keep real init/config behavior and the existing offline Git/GitHub dependencies.
    monkeypatch.setattr(cli, "init_repo", lambda root, **kw: setup.init(**kw))
    argv = ["init", "--repo-root", str(setup.root), "--approver", "human",
            "--no-push", "--no-launchd", "--no-board"]
    return cli.main, argv, calls


def test_claude_only_both_logged_in_choose_codex_and_claude_from_probes(setup, monkeypatch):
    from foragent.config import load_config, resolved_reviewer

    main, argv, probes = init_with_tool_facts(setup, monkeypatch, codex=True, claude=True)
    assert cli_exit(main, argv) == 0
    config = load_config(setup.root)
    assert config.executor == "codex" and resolved_reviewer(config) == "claude"
    assert {tool for tool, args in probes} == {"codex", "claude"}, "Defaults alone do not establish logged-in tool facts"


def test_claude_only_only_claude_chooses_claude_for_execution_and_fresh_review(setup, monkeypatch):
    from foragent.config import load_config, resolved_reviewer

    main, argv, probes = init_with_tool_facts(setup, monkeypatch, codex=False, claude=True)
    assert cli_exit(main, argv) == 0
    config = load_config(setup.root)
    assert config.executor == "claude" and resolved_reviewer(config) == "claude"
    assert {tool for tool, args in probes} == {"codex", "claude"}


def test_claude_only_explicit_executor_wins_over_detected_default(setup, monkeypatch):
    from foragent.config import load_config

    main, argv, _ = init_with_tool_facts(setup, monkeypatch, codex=True, claude=True)
    assert cli_exit(main, [*argv, "--executor", "claude"]) == 0
    assert load_config(setup.root).executor == "claude"


def test_claude_only_neither_logged_in_fails_with_one_line_without_manual_config(setup, monkeypatch, capsys):
    main, argv, _ = init_with_tool_facts(setup, monkeypatch, codex=False, claude=False)
    assert cli_exit(main, argv) != 0
    captured = capsys.readouterr()
    lines = [line for line in (captured.out + captured.err).splitlines() if line.strip()]
    assert len(lines) == 1 and ".foragent.yml" not in lines[0]
    assert not (setup.root / ".foragent.yml").exists()
