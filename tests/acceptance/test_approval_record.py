"""Approval evidence survives a hold; missing evidence refuses resolve without effects.

Draft lock: public tick and CLI entry points, offline tracker/model/transport fakes.
"""

import builtins
from datetime import timedelta
import io
import os

import pytest

from conftest import git_command
from foragent import cli
from foragent.scheduler import tick
from foragent.state import StateStore
from test_orch_review_handoff import bind_cli, make_case, tick_context


def waiting_pr(git_history, tmp_path, monkeypatch, waiting_label):
    case = make_case(git_history, tmp_path, monkeypatch, skill_libraries=[], checks="local")
    case.store.finish_run(1, outcome="blocked")
    case.tracker.remove_labels(1, ["running"])
    case.tracker.add_labels(1, [waiting_label])
    reason = "review-dispute" if waiting_label == "blocked" else "escalated"
    case.tracker.comment(1, f"<!-- foragent:{waiting_label} reason={reason} -->\nNeeds a verdict")
    base = case.history.tip
    case.history.update({"feature.txt": "done"})
    head = case.history.tip
    pr = case.tracker.create_pr(head="foragent/issue-1", base="main", title="Waiting work", body="Review me")
    case.tracker.set_pr_head_sha(pr, head)
    case.tracker.set_pr_files(pr, ["feature.txt"])
    case.tracker.set_pr_checks(pr, "pass")
    git_command(case.history.origin, "update-ref", "refs/heads/foragent/issue-1", head)
    git_command(case.history.origin, "update-ref", "refs/heads/main", base)
    case.history.tip = base
    case.git.fetch()
    main = bind_cli(case, monkeypatch)
    monkeypatch.setattr(cli, "RunContext", lambda **kwargs: case.ctx)
    return case, main, pr, head


@pytest.mark.parametrize("waiting_label", ["needs-human", "blocked"])
@pytest.mark.parametrize("previous_record", ["missing", "older"])
def test_reapproval_during_a_hold_survives_restart_and_allows_resolve(
    git_history, tmp_path, monkeypatch, waiting_label, previous_record,
):
    case, main, pr, head = waiting_pr(git_history, tmp_path, monkeypatch, waiting_label)
    if previous_record == "missing":
        case.store.clear_approval(1)
    case.clock[0] += timedelta(minutes=1)
    body = case.tracker.get_issue(1).body.replace("Implement the feature.", "Finish the reviewed feature.")
    case.tracker.edit_body(1, body)
    case.clock[0] += timedelta(minutes=1)
    case.tracker.remove_labels(1, ["approved"])
    case.tracker.record_label(1, "approved", "human")

    launches = []
    context = tick_context(case, baseline_launcher=lambda *args: None)
    context.launcher = lambda number: launches.append(number)
    tick(context)
    assert case.tracker.get_issue(1).labels == {"approved", waiting_label}
    assert case.tracker.get_issue(pr).state == "open"
    assert case.tracker.pr_head_sha(pr) == head
    assert launches == [], "Recording re-approval must not release the human hold"
    assert case.implementer.calls == [] and case.reviewer.calls == []
    assert case.store.load_approval(1) is not None, "The hold must not prevent durable approval evidence"
    comments = case.tracker.list_comments(1)
    tick(context)
    assert case.tracker.list_comments(1) == comments, "An unchanged approval is recorded once"

    # No in-memory approval object can satisfy the command after this reload.
    case.store = StateStore(case.store.root, now=lambda: case.clock[0])
    case.ctx.store = case.store
    assert main(["orch", "resolve", "1", "--verdict", "pass", "--note", "Reviewed the waiting work"]) == 0
    assert case.tracker.merged_prs == [pr], "A re-approved held issue must reach the ordinary merge path"
    assert case.tracker.get_issue(1).state == "closed"
    assert case.implementer.calls == [] and case.reviewer.calls == []
    assert "approved" in case.tracker.get_issue(1).labels


def watch_file_writes(monkeypatch):
    """Observe filesystem effects without depending on Foragent's storage layout."""
    writes = []
    for module in (builtins, io):
        original = module.open

        def observe(file, mode="r", *args, _open=original, **kwargs):
            if any(flag in mode for flag in "wax+"):
                writes.append(str(file))
            return _open(file, mode, *args, **kwargs)

        monkeypatch.setattr(module, "open", observe)
    original_open = os.open

    def observe_os(path, flags, *args, **kwargs):
        if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND):
            writes.append(str(path))
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", observe_os)
    return writes


def watch_tracker_writes(tracker, monkeypatch):
    effects = []
    for name in ("comment", "add_labels", "remove_labels", "update_issue", "close_issue",
                 "create_pr", "update_pr", "retitle_pr", "close_pr", "merge_pr"):
        original = getattr(tracker, name)

        def observe(*args, _name=name, _method=original, **kwargs):
            effects.append((_name, args, kwargs))
            return _method(*args, **kwargs)

        monkeypatch.setattr(tracker, name, observe)
    return effects


@pytest.mark.parametrize("verdict", ["pass", "fail"])
def test_resolve_without_an_approval_record_refuses_before_any_write(
    git_history, tmp_path, monkeypatch, capsys, kicks, verdict,
):
    case, main, pr, head = waiting_pr(git_history, tmp_path, monkeypatch, "needs-human")
    case.store.clear_approval(1)
    before_issue = case.tracker.get_issue(1)
    before_pr = case.tracker.get_issue(pr)
    before_comments = {n: case.tracker.list_comments(n) for n in (1, pr)}
    before_events = case.tracker.label_events(1)
    before_files = {p.relative_to(case.store.root): p.read_bytes()
                    for p in case.store.root.rglob("*") if p.is_file()}
    before_kicks = list(kicks)
    capsys.readouterr()
    with monkeypatch.context() as observed:
        writes = watch_file_writes(observed)
        tracker_writes = watch_tracker_writes(case.tracker, observed)
        code = main(["orch", "resolve", "1", "--verdict", verdict, "--note", "Reviewed the waiting work"])
    output = capsys.readouterr()
    assert code != 0, f"Missing approval evidence must refuse resolve: {output.out} {output.err}"
    message = output.err.casefold().replace("_", " ")
    assert "approval" in message and "re-approve" in message, output.err
    assert "missing" in message or "no " in message, output.err
    assert writes == [], f"Refusal must precede every file write: {writes}"
    assert tracker_writes == [], f"Refusal must precede every tracker write: {tracker_writes}"
    assert case.tracker.get_issue(1) == before_issue
    assert case.tracker.get_issue(pr) == before_pr
    assert {n: case.tracker.list_comments(n) for n in (1, pr)} == before_comments
    assert case.tracker.label_events(1) == before_events
    assert {p.relative_to(case.store.root): p.read_bytes()
            for p in case.store.root.rglob("*") if p.is_file()} == before_files
    assert case.tracker.pr_head_sha(pr) == head
    assert case.tracker.merged_prs == [] and kicks == before_kicks
    assert case.implementer.calls == [] and case.reviewer.calls == []
