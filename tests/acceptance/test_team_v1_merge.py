"""Carry the actual verified base to the last remote merge boundary."""
import json
from dataclasses import replace
import pytest

from foragent.runner import run_issue
from foragent.tracker import GhTracker, TrackerError
from test_orch_merge import premerge_case
from test_orch_review_handoff import make_case
from team_v1_support import invoke, legacy, person, snapshot


@pytest.mark.parametrize("proof", ["moved", "unreadable", "missing"])
def test_remote_merge_refuses_base_moved_on_another_machine(proof):
    old, new, head = "a" * 40, "b" * 40, "c" * 40
    calls = []
    def gh(args, input=None):
        calls.append(args)
        if args[:2] == ["pr", "view"]:
            return json.dumps({"title": "Feature", "baseRefName": "main", "headRefOid": head})
        if args[:1] == ["api"]:
            if proof == "unreadable":
                raise TrackerError("Remote base unavailable (HTTP 503)")
            return json.dumps({"object": {"sha": new}, "sha": new, "commit": {"sha": new}})
        return ""
    tracker = GhTracker("owner/repo", run=gh)
    # Omit the future keyword on old main so failure proves an unsafe merge,
    # rather than failing at Python argument binding.
    import inspect
    kw = {"expected_base_sha": old} if proof != "missing" and "expected_base_sha" in inspect.signature(tracker.merge_pr).parameters else {}
    try:
        tracker.merge_pr(7, expected_head_sha=head, subject="Feature", **kw)
    except (TrackerError, TypeError) as exc:
        assert "base" in str(exc).lower()
    else:
        pytest.fail("Absent, unreadable or stale base proof must never authorize a merge")
    assert not any(a[:2] == ["pr", "merge"] for a in calls), calls


@pytest.mark.parametrize("entry", ["implementation", "reviewed"])
def test_runner_delivers_verified_base_and_head_to_merge(git_history, tmp_path, monkeypatch, entry):
    if entry == "implementation":
        case = make_case(git_history, tmp_path, monkeypatch, review="fast")
    else:
        from conftest import git_command
        from test_orch_verbs import blocked_case
        case, main = blocked_case(git_history, tmp_path, monkeypatch)
        base = case.history.tip
        case.history.update({"feature.txt": "done"})
        head = case.history.tip
        git_command(case.history.origin, "update-ref", "refs/heads/foragent/issue-1", head)
        git_command(case.history.origin, "update-ref", "refs/heads/main", base)
        case.history.tip = base
        case.git.fetch()
        pr = case.tracker.create_pr(head="foragent/issue-1", base="main", title="Feature", body="Review")
        case.tracker.set_pr_head_sha(pr, head)
        case.tracker.set_pr_files(pr, ["feature.txt"])
    person(case.tracker, monkeypatch, "alice")
    legacy(case.ctx.config)
    case.tracker.add_labels(1, ["by:alice"])
    base, effects = case.history.tip, []
    merge = case.tracker.merge_pr
    def boundary(n, *, expected_head_sha, expected_base_sha=None, **kw):
        effects.append((n, expected_head_sha, expected_base_sha))
        return merge(n, expected_head_sha=expected_head_sha, **kw)
    monkeypatch.setattr(case.tracker, "merge_pr", boundary)
    if entry == "implementation":
        assert run_issue(1, case.ctx) == "merged"
    else:
        assert main(["orch", "rerun-review", "1"]) == 0
    assert len(effects) == 1 and effects[0][2] == base, effects
    assert effects[0][1] == case.tracker.pr_head_sha(effects[0][0])


def test_orchestrator_passes_its_checked_base(git_history, tmp_path, monkeypatch):
    case, main, pr, head = premerge_case(git_history, tmp_path, monkeypatch)
    person(case.tracker, monkeypatch, "alice")
    legacy(case.ctx.config)
    case.tracker.add_labels(1, ["by:alice"])
    base, effects = case.history.tip, []
    merge = case.tracker.merge_pr
    def boundary(n, *, expected_head_sha, expected_base_sha=None, **kw):
        effects.append((n, expected_head_sha, expected_base_sha))
        return merge(n, expected_head_sha=expected_head_sha, **kw)
    monkeypatch.setattr(case.tracker, "merge_pr", boundary)
    assert main(["orch", "merge", str(pr), "--approved-by", "alice"]) == 0
    assert effects == [(pr, head, base)]


def test_orchestrator_refuses_foreign_pr_merge(git_history, tmp_path, monkeypatch, capsys):
    case, main, pr, _ = premerge_case(git_history, tmp_path, monkeypatch)
    person(case.tracker, monkeypatch, "alice")
    legacy(case.ctx.config)
    case.tracker.add_labels(1, ["by:bob"])
    before = snapshot(case)
    code, output = invoke(main, ["orch", "merge", str(pr), "--approved-by", "alice"], capsys)
    assert code != 0 and "bob" in output, output
    assert snapshot(case) == before
