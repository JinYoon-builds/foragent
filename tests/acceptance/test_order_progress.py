"""Acceptance test for order-progress. Locked by Foragent; do not edit."""

from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from test_local_view import record_gh
from test_skills_autosync import cli_exit, isolated_home


def offline_local_cli(tmp_path, monkeypatch):
    """Real CLI/config/state, with user and machine files confined to this test."""
    from foragent import cli, executors, runner
    from foragent.state import StateStore

    root = tmp_path / "project"
    root.mkdir()
    (root / ".foragent.yml").write_text("repo: owner/repo\nskill_libraries: []\n")
    state_home = tmp_path / "home/.local/state"
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))
    state_root = state_home / "foragent/owner__repo"
    monkeypatch.setattr(StateStore, "for_repo", classmethod(
        lambda cls, repo, **kw: cls(state_root, **kw)))
    monkeypatch.chdir(root)

    def no_executor(*args, **kwargs):
        pytest.fail("Local progress, notes, settings and tidy must not call an executor/LLM")

    monkeypatch.setattr(executors, "executor_for", no_executor)
    monkeypatch.setattr(runner, "executor_for", no_executor)
    monkeypatch.setattr(executors.CodexExecutor, "run", no_executor)
    monkeypatch.setattr(executors.ClaudeExecutor, "run", no_executor)
    run = subprocess.run

    def local_run(argv, *args, **kwargs):
        if isinstance(argv, (list, tuple)) and argv:
            assert Path(argv[0]).name not in {
                "codex", "claude", "orca", "tmux", "osascript", "open", "wezterm", "kitty",
            }, f"Local facts must not start an LLM or control a real terminal: {argv}"
        return run(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", local_run)
    calls = record_gh(monkeypatch)
    return SimpleNamespace(main=cli.main, root=root, store=StateStore(state_root),
                           state_home=state_home, gh_calls=calls)


def progress_command(case, *args):
    return cli_exit(case.main, ["order", "progress", *args, "--repo-root", str(case.root)])


def read_progress(case):
    from foragent.state import StateStore

    # Reopen on every read: the view must not depend on the writer's process.
    reader = getattr(StateStore(case.store.root), "order_progress", None)
    assert callable(reader), "StateStore.order_progress() must expose persisted order facts"
    return reader()


def test_order_progress_every_step_persists_facts_and_counts_unfilled_cells(tmp_path, monkeypatch):
    import json

    case = offline_local_cli(tmp_path, monkeypatch)
    assert progress_command(case, "start", "--title", "Ship the lane view") == 0
    state = read_progress(case)
    assert {"title", "step", "ledger", "chunks", "cells", "tests", "published", "cells_left"} <= state.keys()
    assert state["title"] == "Ship the lane view" and state["step"] == "start"
    assert not state["ledger"] and not state["chunks"] and not state["published"]
    for decision in ("D57: open at order start", "D64: facts drive bubbles"):
        assert progress_command(case, "ledger", "--add", decision) == 0
        assert decision in read_progress(case)["ledger"]
        assert read_progress(case)["step"] == "ledger"
    for chunk in ("Progress", "Settings"):
        assert progress_command(case, "chunk", "--add", chunk) == 0
        assert chunk in read_progress(case)["chunks"]
        assert read_progress(case)["step"] == "chunk"
    assert read_progress(case)["cells_left"] == 10
    question = "Which settings belong to the team?"
    assert progress_command(case, "cell", "--chunk", "Settings", "--slot", "lane", "--ask", question) == 0
    assert question in json.dumps(read_progress(case)["cells"], ensure_ascii=False)
    assert read_progress(case)["cells_left"] == 10, "An unanswered question still leaves a cell empty"
    left = 10
    for chunk in ("Progress", "Settings"):
        for slot in ("goal", "out_of_scope", "done_when", "lane", "skills"):
            assert progress_command(case, "cell", "--chunk", chunk, "--slot", slot, "--filled") == 0
            left -= 1
            assert read_progress(case)["cells_left"] == left
            assert read_progress(case)["step"] == "cell"
    for done in (1, 2):
        assert progress_command(case, "tests", "--done", str(done), "--total", "2") == 0
        assert read_progress(case)["tests"] == {"done": done, "total": 2}
        assert read_progress(case)["step"] == "tests"
    for step in ("check", "approval"):
        assert progress_command(case, step) == 0
        assert read_progress(case)["step"] == step
    for issue in (51, 52):
        assert progress_command(case, "publish", "--issue", str(issue)) == 0
        assert issue in read_progress(case)["published"]
        assert read_progress(case)["step"] == "publish"
    assert read_progress(case)["published"] == [51, 52]
    assert read_progress(case)["ledger"] == ["D57: open at order start", "D64: facts drive bubbles"]
    assert read_progress(case)["chunks"] == ["Progress", "Settings"]
    assert case.gh_calls == []


def test_order_progress_invalid_slot_preserves_state_and_end_clears_it(tmp_path, monkeypatch):
    case = offline_local_cli(tmp_path, monkeypatch)
    assert progress_command(case, "start", "--title", "An unfinished order") == 0
    assert progress_command(case, "chunk", "--add", "Progress") == 0
    before = read_progress(case)
    assert progress_command(case, "cell", "--chunk", "Progress", "--slot", "unknown", "--filled") != 0
    assert read_progress(case) == before
    assert progress_command(case, "end") == 0
    assert read_progress(case) is None
    assert case.gh_calls == []


def test_order_progress_missing_state_is_none_and_repeated_fill_counts_once(tmp_path, monkeypatch):
    case = offline_local_cli(tmp_path, monkeypatch)
    assert read_progress(case) is None
    assert progress_command(case, "start", "--title", "One chunk") == 0
    assert progress_command(case, "chunk", "--add", "Progress") == 0
    for _ in range(2):
        assert progress_command(case, "cell", "--chunk", "Progress", "--slot", "goal", "--filled") == 0
        assert read_progress(case)["cells_left"] == 4
    assert case.gh_calls == []


def test_order_progress_skill_opens_at_start_and_reports_facts_without_bubble_llm():
    import re

    text = " ".join((Path(__file__).resolve().parents[2] / "skills/fa-order/SKILL.md").read_text().lower().split())
    assert "fa gate watch --open" in text
    assert re.search(r"(?:start|begin|immediately).{0,180}fa gate watch --open|fa gate watch --open.{0,180}(?:start|begin)", text)
    assert "fa order progress" in text
    for step in ("start", "ledger", "chunk", "cell", "tests", "check", "approval", "publish", "end"):
        assert re.search(rf"\b{step}\b", text), step
    assert re.search(r"(?:each|every).{0,70}(?:step|progress)|(?:step|progress).{0,70}(?:each|every)", text)
    assert re.search(r"(?:bubble|speech).{0,180}(?:facts|state|progress)", text)
    assert re.search(r"(?:no|without|never).{0,80}(?:llm|model).{0,80}(?:call|generation)|(?:no|without).{0,80}(?:call|generation).{0,80}(?:llm|model)", text)
