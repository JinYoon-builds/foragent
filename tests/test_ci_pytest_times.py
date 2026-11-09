"""The merge owner's shard-times writer: visible skip reasons and a commit of its own."""

from dataclasses import replace
from datetime import UTC, datetime
import json
import logging
from types import SimpleNamespace

import pytest

from conftest import ACCEPTANCE, git_command
from foragent import ci_pytest_times as shard_times, main_write_requests as writes
from foragent.scheduler import TickReport
from foragent.state import StateStore
from test_order import offline_publication  # noqa: F401

NOW = datetime(2026, 11, 7, 6, tzinfo=UTC)
FILES = [f"tests/acceptance/test_area_{index}.py" for index in range(4)]
MEMORY = ".claude/agent-memory/fa-docs/MEMORY.md"


@pytest.fixture
def case(offline_publication, git_history, tmp_path):
    root, config, tracker, git, calls, origin = offline_publication
    store = StateStore(tmp_path / "state", now=lambda: NOW)
    ctx = SimpleNamespace(repo="owner/repo", repo_root=root, config=replace(config, merge_owner=True),
                          tracker=tracker, git=git, store=store, now=lambda: NOW)
    files = {ACCEPTANCE: None, shard_times.TIMES: "{}\n"}
    files.update({name: "def test_it():\n    assert True\n" for name in FILES})
    git_history.update(files, message="Seed acceptance files")
    git_command(root, "fetch", "origin")
    git_command(root, "merge", "--ff-only", "origin/main")
    return SimpleNamespace(ctx=ctx, root=root, git=git, store=store, origin=origin,
                           start=git_command(origin, "rev-parse", "main"))


def write_junit(case, n=1):
    cases = "".join(f'<testcase classname="x" name="t" file="{name}" time="2.5"/>' for name in FILES)
    path = case.store.root / "runs" / f"issue-{n}-acceptance-baseline.xml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"<testsuites><testsuite>{cases}</testsuite></testsuites>\n", encoding="utf-8")
    return path


def refresh(case):
    report = TickReport()
    shard_times.refresh(case.ctx, report)
    return report.actions


def test_skip_reason_reaches_the_owner_log_once_without_logging_config(case):
    # The owner process prints report.actions to merge-owner.log; logging stays unconfigured there.
    assert not logging.getLogger("foragent").isEnabledFor(logging.INFO)
    passes = [refresh(case) for _ in range(3)]
    reasons = [line for actions in passes for line in actions if "not refreshed" in line]
    assert len(reasons) == 1, passes
    assert shard_times.TIMES in reasons[0] and "no main acceptance JUnit report" in reasons[0]
    assert passes[0] == reasons  # Printed on the first pass, not later.

    report = write_junit(case)
    report.write_text("<broken", encoding="utf-8")  # A new reason is printed again, once.
    passes = [refresh(case) for _ in range(2)]
    reasons = [line for actions in passes for line in actions if "not refreshed" in line]
    assert len(reasons) == 1 and "unreadable" in reasons[0], passes
    assert writes.pending(case.store) == []


def test_refresh_lands_as_its_own_commit_beside_other_main_writes(case):
    memory = case.root / MEMORY
    memory.parent.mkdir(parents=True, exist_ok=True)
    memory.write_text("Docs\n", encoding="utf-8")
    before = writes.capture(case.git, case.ctx.config, [MEMORY], "Memory", "lane docs", store=case.store)
    write_junit(case)
    assert any("queued" in action for action in refresh(case))
    after = case.root / ".claude/agent-memory/fa-orch/MEMORY.md"
    after.parent.mkdir(parents=True, exist_ok=True)
    after.write_text("Orch\n", encoding="utf-8")
    writes.capture(case.git, case.ctx.config, [str(after.relative_to(case.root))], "Orch", "lane orch",
                   store=case.store)
    assert before is not None and len(writes.pending(case.store)) == 3

    writes.drain(case.ctx, TickReport())

    assert writes.pending(case.store) == []
    commits = git_command(case.origin, "rev-list", "--reverse", f"{case.start}..main").split()
    changed = [git_command(case.origin, "diff-tree", "--no-commit-id", "--name-only", "-r", sha).split()
               for sha in commits]
    assert [shard_times.TIMES] in changed, changed
    assert all(names == [shard_times.TIMES] or shard_times.TIMES not in names for names in changed), changed
    assert len(commits) == 3  # The request ends the group before it and starts none after it.
    times = json.loads(git_command(case.origin, "show", f"main:{shard_times.TIMES}"))
    assert times == {name: 2.5 for name in FILES}
