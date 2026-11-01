"""Acceptance test for merge-turn-before-premerge. Locked by Foragent; do not edit.

Drive the public CLI with real premerge orchestration and queue claims. Only
Git/GitHub, the protected-suite process and machine test capacity are faked.
The store observes reservation lifetimes without replacing its real lock.

Break-it: late refusal launches a forbidden suite; reject-all and skip/double
effects fail the successful cases; releasing/reacquiring the reservation
between premerge and merge changes the observed lease. Other targets stay free.
"""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, nullcontext
from datetime import UTC, datetime
import json
from pathlib import Path
import tempfile
from time import monotonic
from types import SimpleNamespace

import pytest

from foragent import capacity, cli, gh, orch
from foragent.config import Config
from foragent.state import StateStore
from foragent.tracker import FakeTracker


NOW = datetime(2026, 11, 1, 9, tzinfo=UTC)
PR, HOLDER = 1298, 1264
HEAD, BASE = "a" * 40, "b" * 40
SUITE = "protected-suite"


class ObservedStore(StateStore):
    """Expose the lifetime of the real shared merge reservation to fake I/O."""

    lease = None

    @contextmanager
    def merge_lock(self):
        with super().merge_lock():
            assert self.lease is None, "A merge reservation must not overlap itself"
            self.lease = object()
            try:
                yield
            finally:
                self.lease = None


class FakeGit:
    """An open PR with a stable head, a stable base and no merge conflicts."""

    def fetch(self, *args):
        pass

    def ref_sha(self, ref):
        return BASE

    def _call(self, args):
        # The real premerge flow fetches the PR object and combines head/base.
        assert args[:1] == ["fetch"] or "merge" in args, args
        return ""

    def add_detached_worktree(self, path, sha):
        path.mkdir(parents=True, exist_ok=True)

    def remove_worktree(self, path):
        # The surrounding TemporaryDirectory owns cleanup, including failures.
        pass

    def at(self, path):
        return self

    def check_publication(self, base):
        pass


@pytest.fixture
def merge_case(tmp_path, monkeypatch):
    root, home, temporary = (tmp_path / name for name in ("repo", "home", "tmp"))
    for path in (root, home, temporary):
        path.mkdir()
    monkeypatch.chdir(root)
    monkeypatch.setenv("HOME", str(home))
    for variable, directory in (
        ("XDG_CONFIG_HOME", "config"), ("XDG_DATA_HOME", "data"),
        ("XDG_STATE_HOME", "state"), ("XDG_CACHE_HOME", "cache"),
    ):
        monkeypatch.setenv(variable, str(home / directory))
    monkeypatch.setenv("FORAGENT_UPDATE_CHECK", "off")
    monkeypatch.setenv("FORAGENT_VIEW", "off")
    monkeypatch.setattr(tempfile, "tempdir", str(temporary))
    # The selected shell command is a single protected suite. It has no pytest
    # result cache, so success requires exactly one external runner invocation.
    config = Config(repo="example/offline", test_command=SUITE, runner_unit_tests="all")
    store = ObservedStore(tmp_path / "state", now=lambda: NOW)
    tracker = FakeTracker(now=lambda: NOW)
    tracker.add_issue(PR - 1, "Merge candidate")
    assert tracker.create_pr(head="feature/merge-turn", base="main", title="Candidate", body="") == PR
    tracker.set_pr_head_sha(PR, HEAD)
    tracker.set_pr_checks(PR, "pass")
    case = SimpleNamespace(store=store, tracker=tracker, suites=[], merges=[])

    def suite_process(argv, *, cwd, **kwargs):
        assert argv == ["/bin/sh", "-c", SUITE]
        assert Path(cwd).is_dir()
        case.suites.append(store.lease)
        return 0, False, "protected suite passed"

    merge = tracker.merge_pr

    def merge_pr(number, *, expected_head_sha, **kwargs):
        case.merges.append((number, expected_head_sha, store.lease, len(case.suites)))
        merge(number, expected_head_sha=expected_head_sha, **kwargs)

    monkeypatch.setattr(cli, "load_config", lambda path: config)
    monkeypatch.setattr(cli, "GitRepo", lambda path: FakeGit())
    monkeypatch.setattr(cli, "GhTracker", lambda repo: tracker)
    monkeypatch.setattr(StateStore, "for_repo", classmethod(lambda cls, repo, **kw: store))
    monkeypatch.setattr(orch, "run_logged", suite_process)
    monkeypatch.setattr(capacity, "hold_test_slot", lambda *a, **kw: nullcontext())
    # RecordingTracker installs a process-wide recorder; restore it at teardown.
    monkeypatch.setattr(gh, "_recorder", None)
    monkeypatch.setattr(tracker, "merge_pr", merge_pr)
    return case


def queue_live_holder(case, target):
    run = case.store.start_run(
        HOLDER, branch=f"foragent/issue-{HOLDER}", worktree=case.store.root / "holder",
    )
    # Seed the existing append-only queue format, not the claim implementation.
    (case.store.root / "merge-queue.jsonl").write_text(
        json.dumps({"n": HOLDER, "started_at": run.started_at, "target": target,
                    "event": "ready", "head_sha": "c" * 40, "at": NOW.isoformat()}) + "\n",
        encoding="utf-8",
    )


def invoke_merge():
    return cli.main(["orch", "merge", str(PR), "--approved-by", "jin", "--at", NOW.isoformat()])


def assert_checked_merge(case, code, output):
    assert code == 0, output
    assert f"#{PR}" in output and "merged" in output.lower(), output
    assert len(case.suites) == 1, "A free turn must run the protected suite exactly once"
    assert case.tracker.merged_prs == [PR], "A successful premerge must merge exactly once"
    assert len(case.merges) == 1
    number, head, lease_at_merge, suites_at_merge = case.merges[0]
    assert (number, head, suites_at_merge) == (PR, HEAD, 1), "Merge only the checked head, after premerge"
    assert case.suites[0] is not None, "Premerge test runner started before the merge turn was held"
    assert lease_at_merge is case.suites[0], "Keep the same merge turn from premerge through merge"
    assert case.store.lease is None, "Release the reservation when the command finishes"


@pytest.mark.parametrize(
    ("target", "lock_held"), [("main", False), ("release", True)],
    ids=["ready-holder", "holder-merging"],
)
def test_live_holder_is_named_without_running_premerge(merge_case, capsys, target, lock_held):
    case = merge_case
    case.tracker.update_pr(PR, base=target, title="Candidate", body="")
    queue_live_holder(case, target)
    started = monotonic()
    delayed = False
    with ThreadPoolExecutor(max_workers=1) as pool:
        # A holder may already be merging. The caller must report that holder
        # while its real shared lock is still occupied, without waiting it out.
        with StateStore.merge_lock(case.store) if lock_held else nullcontext():
            future = pool.submit(invoke_merge)
            try:
                code = future.result(timeout=2)
            except TimeoutError:
                delayed = True
        code = future.result(timeout=2)
    elapsed = monotonic() - started
    captured = capsys.readouterr()
    output = captured.out + captured.err

    assert code != 0, "A live holder must prevent a human merge"
    assert f"#{HOLDER}" in output and target in output, output
    assert not delayed, "Report the busy turn while its holder still owns the merge lock"
    assert elapsed < 2, f"An offline busy-turn refusal took {elapsed:.3f}s"
    assert case.tracker.merged_prs == [] and case.merges == []
    assert not case.tracker.list_comments(PR), "A refused merge must not post approval"
    assert case.suites == [], "Busy merge turn must be refused before invoking any premerge test runner"


@pytest.mark.parametrize("other_holder", [False, True], ids=["empty-turn", "other-target-busy"])
def test_available_target_runs_premerge_and_merges_under_one_turn(merge_case, capsys, other_holder):
    case = merge_case
    if other_holder:
        queue_live_holder(case, "release")
    code = invoke_merge()
    captured = capsys.readouterr()
    assert_checked_merge(case, code, captured.out + captured.err)
