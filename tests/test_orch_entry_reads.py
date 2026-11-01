"""Ownership uses command facts without making local cleanup depend on GitHub."""

from dataclasses import replace
from copy import deepcopy
from datetime import timedelta
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "acceptance"))

from foragent import orch, orch_scope  # noqa: E402
from foragent.config import Config  # noqa: E402
from foragent.state import StateStore  # noqa: E402
from foragent.tracker import FakeTracker, RecordingTracker, TrackerError  # noqa: E402
from conftest import git_command  # noqa: E402
from test_orch_premerge import merge_case  # noqa: E402, F401
from test_orch_stop import _case as stop_case  # noqa: E402
from test_keep_unfinished_work import BRANCH, _assert_saved, _expected_tree, _tree  # noqa: E402
from test_orch_verbs import blocked_case  # noqa: E402
import test_stop_whole_tree as stop_fakes  # noqa: E402


@pytest.fixture
def fake_processes(monkeypatch, tmp_path, no_forbidden_git_commands):
    return stop_fakes.fake_processes.__wrapped__(monkeypatch, tmp_path, no_forbidden_git_commands)


def state_bytes(store):
    return {str(path.relative_to(store.root)): path.read_bytes()
            for path in store.root.rglob("*") if path.is_file()}


def issue_transport(ctx, tracker, read):
    """Intercept public transport reads, leaving FakeTracker's internal lookups alone."""
    class Transport:
        def get_issue(self, number):
            return read(number)

        def __getattr__(self, name):
            return getattr(tracker, name)

    ctx.tracker = RecordingTracker(Transport(), ctx, queue_writes=True)


@pytest.mark.parametrize("failure, cached, paused", [
    ("HTTP 502", True, False), ("HTTP 429 API rate limit exceeded", True, False),
    ("HTTP 502", False, False), ("HTTP 429 API rate limit exceeded", False, True),
])
def test_singleton_cli_stop_kills_and_keeps_wip_before_any_unavailable_read(
    git_history, tmp_path, monkeypatch, fake_processes, capsys, failure, cached, paused,
):
    case, tree, main = stop_case(git_history, tmp_path, monkeypatch, fake_processes)
    if cached:
        case.ctx.tracker.get_issue(1)
    else:
        case.store.drop_missing_github_issue(1)
    assert case.ctx.config.legacy_owner is None
    if paused:
        case.store.pause_github(case.clock[0] + timedelta(minutes=10))
    reads = []

    def unavailable(number):
        reads.append(number)
        assert not {row.pid for row in tree} & fake_processes.live, "GitHub read preceded the local kill"
        raise TrackerError(failure)

    issue_transport(case.ctx, case.tracker, unavailable)
    protected = fake_processes.live - {row.pid for row in tree}
    why = "Keep work during the GitHub outage"
    code = main(["orch", "stop", "1", "--keep-work", "--why", why])
    output = capsys.readouterr()
    assert not {row.pid for row in tree} & fake_processes.live
    assert protected <= fake_processes.live
    run = case.store.get_run(1)
    assert (run.status, run.outcome, run.detail) == ("finished", "stopped", why)
    assert len([run for run in case.store.read_history() if run.outcome == "stopped"]) == 1
    if paused or not cached:
        # A GitHub wait, or an owner nothing can confirm, leaves the WIP on the local branch.
        head = git_command(case.root, "rev-parse", BRANCH)
        assert _tree(case.root, head) == _expected_tree(case)
        assert git_command(case.root, "rev-list", "--count", f"{case.base}..{head}") == "1"
        assert why in git_command(case.root, "log", "-1", "--format=%B", head)
    else:
        _assert_saved(case, why)
    # Shared-label repair can still fail, as on main; the stop must already be complete.
    assert code == 1 and failure in output.err and "Cannot confirm" not in output.err
    assert reads and set(reads) == {1}


VERBS = [
    ["retry", "1", "--hint", "Keep work"], ["park", "1", "--why", "Wait"], ["unpark", "1"],
    ["resolve", "1", "--verdict", "pass", "--note", "Checked"], ["rerun-review", "1"],
    ["escalate", "1", "--summary", "Choose", "--tried", "Checked"],
]


@pytest.mark.parametrize("argv", VERBS, ids=lambda argv: argv[0])
@pytest.mark.parametrize("failure", ["HTTP 429 API rate limit exceeded", "HTTP 502"])
def test_issue_verbs_preserve_their_original_read_error_without_any_effect(
    git_history, tmp_path, monkeypatch, capsys, argv, failure,
):
    case, main = blocked_case(git_history, tmp_path, monkeypatch)
    case.ctx.config = replace(case.ctx.config, legacy_owner="alice")
    before = state_bytes(case.store)
    comments = deepcopy(case.tracker.comments)
    reads = []

    def unavailable(number):
        reads.append(number)
        raise TrackerError(failure)

    issue_transport(case.ctx, case.tracker, unavailable)
    assert main(["orch", *argv]) == 1
    output = capsys.readouterr()
    assert output.err.strip() == f"foragent: {failure}" and output.out == ""
    assert reads == [1] and state_bytes(case.store) == before
    assert case.tracker.comments == comments and case.tracker.merged_prs == []
    assert case.implementer.calls == [] and case.reviewer.calls == []


@pytest.mark.parametrize("owner", ["alice", "bob"])
def test_issue_command_uses_its_existing_fresh_read_instead_of_a_stale_singleton(
    git_history, tmp_path, monkeypatch, capsys, owner,
):
    case, main = blocked_case(git_history, tmp_path, monkeypatch)
    case.ctx.tracker.get_issue(1)  # Earlier operation observed an unlabelled singleton.
    orch_scope.check(case.ctx, 1)
    case.tracker.add_labels(1, [f"by:{owner}"])
    monkeypatch.setattr(case.tracker, "authenticated_login", lambda: "alice")
    before = state_bytes(case.store)
    get_issue, reads = case.tracker.get_issue, []

    def read(number):
        reads.append(number)
        return get_issue(number)

    issue_transport(case.ctx, case.tracker, read)
    code = main(["orch", "retry", "1"])
    output = capsys.readouterr()
    assert reads == [1]
    if owner == "alice":
        assert code == 0 and "queued implementation retry" in output.out
        assert "ready" in get_issue(1).labels and "blocked" not in get_issue(1).labels
    else:
        assert code == 1 and "bob" in output.err and len(output.err.splitlines()) == 1
        assert state_bytes(case.store) == before


def linked_merge(case, owner, legacy):
    case.tracker.add_issue(10, "Feature", labels=[f"by:{owner}"] if owner else [])
    case.ctx.tracker.get_issue(10)
    case.tracker.created_prs = [replace(pr, head="foragent/issue-10", body="Closes #10")
                                for pr in case.tracker.created_prs]
    case.ctx.config = replace(case.ctx.config, legacy_owner=legacy)
    return case


@pytest.mark.parametrize("cached", [False, True])
def test_merge_check_without_a_business_read_keeps_the_stored_singleton_path(tmp_path, cached):
    from types import SimpleNamespace

    tracker, store = FakeTracker(), StateStore(tmp_path / "state")
    tracker.add_issue(7, "Own work")
    if cached:
        store.record_issue(tracker.get_issue(7), {})
    ctx = SimpleNamespace(config=Config(), store=store, tracker=tracker)

    def unavailable(number):
        pytest.fail("Ownership introduced a read for a stored singleton")

    issue_transport(ctx, tracker, unavailable)
    assert orch_scope.merge_check(ctx, 7) is None


@pytest.mark.parametrize("legacy, owner", [(None, None), ("alice", "alice")])
def test_merge_reuses_one_title_read_for_ownership_before_attribution(merge_case, monkeypatch, legacy, owner):
    c = linked_merge(merge_case, owner, legacy)
    monkeypatch.setattr(c.tracker, "authenticated_login", lambda: "alice")
    get_issue, reads = c.tracker.get_issue, []

    def read(number):
        reads.append((number, bool(c.tracker.list_comments(c.pr)), bool(c.tracker.merged_prs)))
        return get_issue(number)

    issue_transport(c.ctx, c.tracker, read)
    assert "merged" in orch.merge(c.ctx, c.pr, approved_by="alice")
    assert c.tracker.merged_prs == [c.pr]
    # The current title at preflight, then the pre-existing post-merge closing observation.
    assert reads == [(10, False, False), (10, True, True)]


def test_merge_fresh_title_facts_refuse_a_new_owner_before_attribution(merge_case, monkeypatch):
    c = linked_merge(merge_case, "alice", "alice")
    monkeypatch.setattr(c.tracker, "authenticated_login", lambda: "alice")
    assert orch_scope.check(c.ctx, 10) is None
    c.tracker.remove_labels(10, ["by:alice"])
    c.tracker.add_labels(10, ["by:bob"])
    with pytest.raises(orch.OrchError, match="worked by bob"):
        orch.merge(c.ctx, c.pr, approved_by="alice")
    assert c.tracker.list_comments(c.pr) == [] and c.tracker.merged_prs == []


@pytest.mark.parametrize("failure", ["HTTP 429 API rate limit exceeded", "HTTP 502"])
def test_merge_title_lookup_preserves_transport_error_before_attribution(merge_case, monkeypatch, failure):
    c = linked_merge(merge_case, None, None)
    error, reads = TrackerError(failure), []

    def unavailable(number):
        reads.append(number)
        raise error

    issue_transport(c.ctx, c.tracker, unavailable)
    with pytest.raises(TrackerError) as raised:
        orch.merge(c.ctx, c.pr, approved_by="alice")
    assert raised.value is error and reads == [10]
    assert c.tracker.list_comments(c.pr) == [] and c.tracker.merged_prs == []
