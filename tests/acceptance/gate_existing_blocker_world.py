"""Gate world for gate-waits-for-existing-blocker (helper, not a test file).

Order blocked-by-existing (Jin r1301, PM r1334). WAITING (#2772) is approved and ready ahead of
time, blocked by BLOCKER (#2552), an issue published before the order. The dependency is
recorded exactly as publication records it (FakeTracker.add_blocked_by). The gate runs through
``scheduler.tick`` with free capacity; the orchestrator inbox is read through
``fa orch inbox --json``.

PM r1334: a blocker counts as finished when its Done when passes on main, whatever its closure
says. Each blocker's Done when is ``test -f proof/<N>.txt``: it fails on main until a main
commit adds that file (``deliver``), and then it passes on any checkout of current main.
Offline: fake tracker, fixture-local Git and shell commands only inside the test directory,
fake process table, no signals.
"""

from dataclasses import replace
from datetime import timedelta
import json
import os
from pathlib import Path
import re
import subprocess

import pytest

from conftest import ACCEPTANCE
from foragent import cli
from foragent.issue import body_hash
from foragent.state import StateStore
from test_scheduler import approve
from test_skills_autosync import cli_exit

WAITING, BLOCKER = 2772, 2552          # the 11-09 pair: #2772 waited on #2552
OTHER_WAITING, OTHER_BLOCKER = 2773, 2553
LANES = {lane: {"paths": [f"{lane}/**"]} for lane in ("orch", "view")}


def proof_path(number):
    return f"proof/{number}.txt"


def blocker_body(number):
    """An existing issue body whose Done when passes on main only once its proof file is there."""
    return (
        "<!-- foragent:v1 -->\n## Goal\nThe earlier work.\n## Out of scope\nOther work.\n"
        f"## Done when\n```sh\ntest -f {proof_path(number)}\n```\n"
    )


@pytest.fixture
def offline_gate_world(tmp_path, monkeypatch, isolated_home, offline_acceptance):
    """Git and Done when commands may run, but only inside this test's directory; no signals."""
    popen = subprocess.Popen
    root = tmp_path.resolve()

    def local_only(argv, *args, **kwargs):
        cwd = kwargs.get("cwd")
        assert cwd is not None and Path(cwd).resolve().is_relative_to(root), (
            f"Commands in this world must run inside the test directory: {argv!r} cwd={cwd!r}")
        return popen(argv, *args, **kwargs)

    def no_signal(*args, **kwargs):
        pytest.fail("Gate blocker tests must never signal real processes")

    monkeypatch.setattr(subprocess, "Popen", local_only)
    monkeypatch.setattr(os, "kill", no_signal)
    monkeypatch.setattr(os, "killpg", no_signal)


def make_world(ctx, monkeypatch, git_history, body, *, pairs=((WAITING, BLOCKER, "orch"),)):
    """Each (waiting, blocker, lane): blocker is an existing open issue whose Done when fails on
    main; waiting is approved, ready in its own lane, and blocked by it. Nothing runs yet."""
    ctx.config = replace(ctx.config, repo=ctx.repo, max_parallel=None, skill_libraries=[],
                         test_command="true", lanes=LANES)
    clock = [ctx.now()]
    ctx.now = lambda: clock[0]
    ctx.clock = clock
    ctx.history = git_history
    ctx.start_runs = True
    ctx.launched = []

    def launch(number):
        ctx.launched.append(number)
        return 7000 + number

    ctx.launcher = launch
    monkeypatch.setattr(cli, "load_config", lambda root: ctx.config)
    monkeypatch.setattr(StateStore, "for_repo", classmethod(
        lambda cls, repo, **kwargs: cls(ctx.store.root, now=ctx.now)))
    faithful_dependencies(ctx.tracker, monkeypatch)
    for waiting, blocker, lane in pairs:
        ctx.tracker.add_issue(blocker, f"Earlier published work #{blocker}", blocker_body(blocker),
                              labels=[])
        approve(ctx, waiting, body=body, labels=[f"lane:{lane}", "ready"])
        ctx.store.save_approval(
            waiting, body=body, body_sha=body_hash(body),
            acceptance_blobs=ctx.git.blob_shas("origin/main", [ACCEPTANCE]),
            approved_by="human", approved_at=ctx.now().isoformat(),
        )
        ctx.tracker.add_blocked_by(waiting, blocker)
    return ctx


def faithful_dependencies(tracker, monkeypatch):
    """GitHub's blocked_by API returns each blocker's state *and* state_reason
    (GhTracker.blocked_by reads both). FakeTracker.blocked_by refreshes only the state, so this
    world answers like GitHub: the blocker's current state and close reason."""
    real = tracker.blocked_by

    def blocked_by(n):
        return [
            replace(dep, state=tracker.issues[dep.number].state,
                    state_reason=tracker.issues[dep.number].state_reason)
            if dep.number in tracker.issues else dep
            for dep in real(n)
        ]

    monkeypatch.setattr(tracker, "blocked_by", blocked_by)


def advance(ctx, minutes):
    ctx.clock[0] += timedelta(minutes=minutes)


def deliver(ctx, number=BLOCKER):
    """A main commit after which the blocker's Done when passes on main."""
    ctx.history.update({proof_path(number): "delivered\n"}, message=f"Deliver #{number}")


def close_blocker(ctx, reason, number=BLOCKER):
    """reason "completed" is a merge; anything else (not_planned, duplicate, None) is not."""
    ctx.tracker.close_issue(number, reason=reason)


def started(ctx, number=WAITING):
    return number in ctx.launched or any(run.number == number for run in ctx.store.active_runs())


def inbox_rows(capsys, ctx):
    capsys.readouterr()
    code = cli_exit(cli.main, ["orch", "inbox", "--json", "--repo-root", str(ctx.repo_root)])
    out = capsys.readouterr().out
    assert code == 0, "fa orch inbox --json must succeed"
    return json.loads(out)


UNDELIVERED_WORDS = re.compile(
    r"(without\s+(being\s+)?deliver|not\s+(been\s+)?deliver|never\s+deliver|undelivered|"
    r"done\s+when\s+(still\s+)?(fails|failing|does\s+not\s+pass|doesn'?t\s+pass))", re.I)
DELIVERED_WORDS = re.compile(
    r"((looks|appears|seems)\s+(to\s+be\s+|to\s+have\s+been\s+)?delivered|"
    r"already\s+(delivered|pass(es)?)|done\s+when\s+(already\s+)?passes)", re.I)


def _text(row):
    return row["text"] if isinstance(row.get("text"), str) else json.dumps(row, ensure_ascii=False)


def _names(text, number):
    return re.search(rf"#{number}(?!\d)", text) is not None


def mentions_undelivered_close(row, waiting=WAITING, blocker=BLOCKER):
    """A request naming the waiting issue, its blocker, and that the blocker closed without
    delivering (its Done when still fails on main)."""
    text = _text(row)
    return (_names(text, waiting) and _names(text, blocker)
            and UNDELIVERED_WORDS.search(text) is not None)


def mentions_looks_delivered(row, blocker=BLOCKER):
    """A request saying the still-open blocker looks delivered (its Done when passes on main)."""
    text = _text(row)
    return (_names(text, blocker) and DELIVERED_WORDS.search(text) is not None
            and UNDELIVERED_WORDS.search(text) is None)


def mentions_issue(row, number):
    return _names(_text(row), number)
