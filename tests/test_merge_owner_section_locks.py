"""Real train/single landings must reach git, GitHub and push without flock."""

from contextlib import contextmanager

from helpers.merge_world import world  # noqa: F401
from test_merge_owner_proto import proto  # noqa: F401
from helpers.merge_batch import batched, queue_workers  # noqa: F401
import test_merge_owner_proto as support
from test_section_locks import LockGuard
from foragent.git import GitRepo


@contextmanager
def guarded(case, monkeypatch):
    guard = LockGuard(monkeypatch)
    calls = []
    real_call = GitRepo._call

    def git_call(self, args):
        calls.append(f"git {args[0]}")
        guard.check(calls[-1])
        return real_call(self, args)

    monkeypatch.setattr(GitRepo, "_call", git_call)
    for name in dir(type(case.tracker)):
        original = getattr(case.tracker, name, None)
        if name.startswith("_") or not callable(original):
            continue

        def github(*args, __original=original, __name=name, **kwargs):
            calls.append(f"GitHub {__name}")
            guard.check(calls[-1])
            return __original(*args, **kwargs)

        monkeypatch.setattr(case.tracker, name, github)
    try:
        yield calls
    finally:
        guard.active = False
        assert not guard.violations, guard.report()
        assert any(call.startswith("git ") for call in calls)
        assert any(call.startswith("GitHub ") for call in calls)
        assert guard.starts, "real git subprocesses must run"


def test_single_pr_landing_runs_git_and_github_without_flock(proto, monkeypatch):
    case = proto
    support.queue_workers(case, [5])
    with guarded(case, monkeypatch) as calls:
        support.gate(case)
    assert len(case.tracker.merged_prs) == 1
    assert "GitHub merge_pr" in calls
    assert not case.store.active_runs()


def test_train_landing_runs_final_fetch_github_and_target_push_without_flock(batched, monkeypatch):
    case = batched
    queue_workers(case, [5, 1])
    with guarded(case, monkeypatch) as calls:
        support.gate(case)
    assert len(case.target_pushes) == 1
    assert len(case.store.owner_merge_facts()) == 2
    assert "git fetch" in calls and "git push" in calls
