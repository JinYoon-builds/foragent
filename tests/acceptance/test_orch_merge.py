"""Acceptance test for orch-merge. Locked by Foragent; do not edit."""

import pytest


def premerge_case(git_history, tmp_path, monkeypatch, *, failing=False, conflict=False, ci="pass"):
    from dataclasses import replace

    from conftest import git_command
    from test_orch_review_handoff import bind_cli, make_case

    case = make_case(git_history, tmp_path, monkeypatch, review="careful")
    case.history.update({"feature.txt": "old"})
    common = case.history.tip
    case.history.update({"feature.txt": "PR version", "full-suite-ok": None if failing else "yes"})
    head = case.history.tip
    git_command(case.history.origin, "update-ref", "refs/heads/foragent/issue-1", head)
    case.history.tip = common
    git_command(case.history.origin, "update-ref", "refs/heads/main", common)
    case.history.update({"feature.txt": "base version"} if conflict else {"base.txt": "new base"})
    case.git.fetch()
    case.ctx.config = replace(case.config, test_command="test -f full-suite-ok")
    pr = case.tracker.create_pr(head="foragent/issue-1", base="main", title="Feature", body="Review")
    case.tracker.set_pr_head_sha(pr, head)
    case.tracker.set_pr_checks(pr, ci)
    return case, bind_cli(case, monkeypatch), pr, head


@pytest.mark.parametrize("failing,conflict,ci,ready", [
    (False, False, "pass", True), (True, False, "pass", False),
    (False, True, "pass", False), (False, False, "pending", False),
    (False, False, "none", True),
])
def test_premerge_checks_pr_head_tests_conflicts_and_ci_without_github_writes(
    git_history, tmp_path, monkeypatch, capsys, failing, conflict, ci, ready,
):
    import json

    case, main, pr, head = premerge_case(git_history, tmp_path, monkeypatch,
                                        failing=failing, conflict=conflict, ci=ci)
    before = (dict(case.tracker.issues), list(case.tracker.created_prs), dict(case.tracker.comments))
    (case.root / "unrelated-local.txt").write_text("keep me")
    result = main(["orch", "premerge", str(pr), "--json"])
    assert result in (0, 1)
    payload = json.loads(capsys.readouterr().out)
    assert payload["pr"] == pr and payload["ready"] is ready
    assert payload["tests"] == ("fail" if failing else "pass")
    assert payload["ci"] == ci
    assert payload["conflicts"] == (["feature.txt"] if conflict else [])
    assert (dict(case.tracker.issues), list(case.tracker.created_prs), dict(case.tracker.comments)) == before
    assert case.tracker.merged_prs == []
    assert (case.root / "unrelated-local.txt").read_text() == "keep me"


@pytest.mark.parametrize("failing,conflict,ci", [(True, False, "pass"), (False, True, "pass"),
                                                (False, False, "pending")])
def test_merge_refuses_any_unready_premerge(git_history, tmp_path, monkeypatch, failing, conflict, ci):
    case, main, pr, head = premerge_case(git_history, tmp_path, monkeypatch,
                                        failing=failing, conflict=conflict, ci=ci)
    assert main(["orch", "merge", str(pr), "--approved-by", "jin"]) != 0
    assert case.tracker.merged_prs == []
    assert not any("Approved by" in c.body for c in case.tracker.list_comments(pr))


def test_approved_merge_posts_attribution_logs_event_and_matches_head(git_history, tmp_path, monkeypatch):
    import json

    case, main, pr, head = premerge_case(git_history, tmp_path, monkeypatch)
    merged = []
    merge = case.tracker.merge_pr

    def checked(number, *, expected_head_sha, method="squash"):
        merged.append((number, expected_head_sha))
        return merge(number, expected_head_sha=expected_head_sha, method=method)

    monkeypatch.setattr(case.tracker, "merge_pr", checked)
    at = "2026-10-21T12:00:00+00:00"
    assert main(["orch", "merge", str(pr), "--approved-by", "jin", "--at", at]) == 0
    assert merged == [(pr, head)]
    assert any(c.body == f"Approved by jin in the orchestrator ({at}). Pre-merge: tests pass, no conflicts, CI pass."
               for c in case.tracker.list_comments(pr))
    events = [json.loads(line) for line in (case.store.root / "orch-events.jsonl").read_text().splitlines()]
    assert events[-1]["at"] and events[-1]["kind"] and events[-1]["summary"]
