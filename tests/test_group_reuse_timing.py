"""Group reuse holds wherever a follower meets a moved base (group-reuse).

A result-neutral commit can land at any base-move point: before the follower's
pre-lock fetch, or while it waits for the merge lock. Every point must ask the
same reuse question with the same neutral rule, or the follower changes its
checked head and runs its checks again.
"""

from collections import Counter
from pathlib import Path
import shlex
import sys

import pytest

sys.path.insert(0, str(Path(__file__).parent / "acceptance"))

from test_merge_group import (  # noqa: E402,F401
    contend, group_case, isolated_home, offline_acceptance, prepare, publish_case,
)


@pytest.fixture(autouse=True)
def isolated_runner_process_pause(monkeypatch):
    # Real runners each own a process; group_case simulates them with threads.
    # Their overlapping deadline scopes can exit out of stack order. Keep that
    # process-wide fallback inside this simulation, including on test failure.
    from foragent import processes

    previous = processes.current_pause()
    with monkeypatch.context() as patch:
        patch.setattr(processes, "_process_paused", None)
        yield
    assert processes.current_pause() is previous, "Simulated runners leaked their process pause"


def _probe_counts(case):
    done = [row for row in case.probes if row.armed and "tools/group_probe.py" in row.command]
    return Counter(int(shlex.split(row.command)[-1]) for row in done)


def _ready_heads(case):
    return {row["n"]: row["head_sha"] for _, row in case.appends if row["event"] == "ready"}


def test_neutral_commit_during_lock_acquisition_keeps_the_checked_head(group_case):
    case = group_case
    prepare(case, (1, 4))
    moved = []

    def late_docs(n):
        # After #1 merged, a docs-only commit lands while #4 waits for the lock.
        if n == 4 and case.merges and not moved:
            with case.external():
                case.history.update({"docs/late-notes.md": "Late notes\n"})
            moved.append(case.history.tip)

    contend(case, before_acquire=late_docs)
    assert moved, "The late neutral commit was never injected"
    assert [row.issue for row in case.merges] == [1, 4]
    assert _probe_counts(case) == Counter((1, 4)), "A neutral move during lock acquisition reran checks"
    ready = _ready_heads(case)
    assert all(row.head == ready[row.issue] for row in case.merges), "Merge exactly the checked heads"
    assert all(case.outcomes[n] == "merged" for n in (1, 4))


def test_source_commit_during_lock_acquisition_still_rechecks(group_case):
    case = group_case
    prepare(case, (1, 4))
    moved = []

    def late_source(n):
        if n == 4 and case.merges and not moved:
            with case.external():
                case.history.update({"src/late_outsider.py": "VALUE = 2\n"})
            moved.append(case.history.tip)

    contend(case, before_acquire=late_source)
    assert moved
    assert [row.issue for row in case.merges] == [1, 4]
    assert _probe_counts(case)[4] > 1, "A result-affecting move must void reuse and recheck #4"
    assert all(case.outcomes[n] == "merged" for n in (1, 4))


def test_neutral_commit_during_the_leaders_lock_acquisition_keeps_one_check(group_case):
    case = group_case
    prepare(case, (1, 4))
    moved = []

    def late_docs(n):
        # The leader ran the joint check; a docs-only commit lands before its lock.
        if n == 1 and not case.merges and not moved:
            with case.external():
                case.history.update({"docs/leader-notes.md": "Leader notes\n"})
            moved.append(case.history.tip)

    contend(case, before_acquire=late_docs)
    assert moved, "The late neutral commit was never injected"
    assert [row.issue for row in case.merges] == [1, 4]
    assert _probe_counts(case) == Counter((1, 4)), "The leader's neutral move repeated the joint suite"
    ready = _ready_heads(case)
    assert all(row.head == ready[row.issue] for row in case.merges), "The leader must merge its checked head"
    assert all(case.outcomes[n] == "merged" for n in (1, 4))


def test_source_commit_during_the_leaders_lock_acquisition_still_rechecks(group_case):
    case = group_case
    prepare(case, (1, 4))
    moved = []

    def late_source(n):
        if n == 1 and not case.merges and not moved:
            with case.external():
                case.history.update({"src/leader_outsider.py": "VALUE = 3\n"})
            moved.append(case.history.tip)

    contend(case, before_acquire=late_source)
    assert moved
    assert [row.issue for row in case.merges] == [1, 4]
    assert _probe_counts(case)[1] > 1, "A result-affecting move must void the leader's pass"
    assert all(case.outcomes[n] == "merged" for n in (1, 4))
