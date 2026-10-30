"""Acceptance test for rules-readers-orch. Locked by Foragent; do not edit."""

from datetime import UTC, datetime, timedelta
import shutil
from types import SimpleNamespace

import pytest

from foragent import orch_dream, orch_dream_worker, usage_events
from foragent.config import Config
from foragent.executors import ExecResult
from foragent.state import StateStore
from test_orch_refresh import accept_note, fa_ok, refresh_case, seed_session  # noqa: F401
from test_skills_autosync import isolated_home  # noqa: F401


RULE = "RULES_CURRENT_CONTEXT_6d21"
HISTORY = "DECISIONS_HISTORY_CONTEXT_b8a4"


def write_documents(root, rules_present):
    docs = root / "docs"
    docs.mkdir(exist_ok=True)
    (docs / "decisions.md").write_text(f"# Decisions\n\n{HISTORY}\n", encoding="utf-8")
    if rules_present:
        (docs / "rules.md").write_text(f"# Rules\n\n{RULE}\n", encoding="utf-8")


@pytest.mark.parametrize("rules_present", [True, False], ids=["current-rules", "adopted-fallback"])
def test_refresh_launch_receives_selected_document_context(refresh_case, capsys, rules_present):
    case = refresh_case
    write_documents(case.root, rules_present)
    seed_session(case)
    stored = accept_note(case, capsys)
    case.clock[0] += timedelta(seconds=1)

    fa_ok(case, capsys, "refresh", "--ready")

    prompt = case.spawned[-1]["prompt"]
    selected = "docs/rules.md" if rules_present else "docs/decisions.md"
    reading = next(line for line in prompt.splitlines() if line.startswith("2. "))
    assert selected in reading, "Refresh item 2 must identify the selected current-rule source"
    assert (RULE if rules_present else HISTORY) in prompt, "Refresh must supply the selected document's actual context"
    if rules_present:
        assert HISTORY not in prompt, "History is consulted for reasons, not supplied as the current rules"
        # The history path may still be named as an optional source of reasons.
        if "docs/decisions.md" in prompt:
            assert "reason" in prompt.lower()
    assert str(stored) in prompt and "fa orch queue" in prompt and "fa orch inbox --json" in prompt


@pytest.mark.parametrize("rules_present", [True, False], ids=["current-rules", "adopted-fallback"])
def test_dream_worker_uses_selected_document_as_authority(git_history, tmp_path, monkeypatch, rules_present):
    root = git_history.clone
    write_documents(root, rules_present)
    config = Config(lanes={"orch": {"agent": "fa-orch", "paths": ["src/**"]}})
    store = StateStore(tmp_path / "state")
    worktree = orch_dream.worktree_path(store, "orch")
    store.start_dream("orch", worktree=worktree)
    turns = []

    def prepare(*args):
        worktree.mkdir(parents=True)
        shutil.copytree(root / "docs", worktree / "docs")
        return SimpleNamespace(commit_paths=lambda *args: pytest.fail("An unchanged dream has nothing to commit"))

    def execute(**kwargs):
        turns.append(kwargs)
        return ExecResult(0, False, "No memory changes needed.", kwargs["log_path"])

    monkeypatch.setattr(orch_dream_worker, "_dream_worktree", prepare)
    monkeypatch.setattr(usage_events, "record_from_log", lambda *args: None)
    # --repo-root can select a repository other than the launcher's current folder.
    launcher = tmp_path / "unrelated-launcher"
    launcher.mkdir()
    monkeypatch.chdir(launcher)
    ctx = SimpleNamespace(repo_root=root, store=store, config=config, now=lambda: datetime.now(UTC),
                          git=SimpleNamespace(remove_worktree=lambda path: None),
                          executor_factory=lambda name: SimpleNamespace(run=execute),
                          poll_hook=lambda execute, poll: execute())

    assert orch_dream_worker.run_dream("orch", ctx) == 0
    assert len(turns) == 1
    prompt = turns[0]["prompt"]

    selected = "docs/rules.md" if rules_present else "docs/decisions.md"
    assert selected in prompt, "Dream authority must use current rules, with the adopted-project fallback"
    assert (RULE if rules_present else HISTORY) in prompt, "Dream must receive the selected document's actual context"
    authority = [line for line in prompt.splitlines()
                 if "authoritative" in line.lower() or "conflict" in line.lower() or "disagree" in line.lower()]
    assert authority and all(selected in line for line in authority)
    if rules_present:
        assert HISTORY not in prompt
        assert all("docs/decisions.md" not in line for line in authority), "Every conflict instruction must use current rules"
    assert ".claude/agent-memory/fa-orch/MEMORY.md" in prompt
