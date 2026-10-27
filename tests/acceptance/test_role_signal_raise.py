"""Acceptance test for role-signal-raise. Locked by Foragent; do not edit."""

from datetime import timedelta

import pytest

from test_local_truth import record_gh
from test_notify_blocked import notify_blocked_capture
from test_role_signals import call_event, completed, signal_case
from test_role_source import NOW, write_role_snapshot
from test_skills_autosync import cli_exit, isolated_home  # noqa: F401 (fixture)
from test_tick_failure_alert import run_tick, tick_case  # noqa: F401 (fixture)

# Chosen public contract:
# - foragent.orch_role_raise.raise_due(store, repo_root, now) -> list of the inbox
#   requests it added. The gate tick calls it once per tick and is its only writer.
# - A raised request is an ordinary orchestrator inbox request with from="gate",
#   role=<name> and signal=<the {paths, kinds} item from the roles source>.
# - fa orch signal-answer <request id> called|not-now --why TEXT records the answer
#   and marks the request done; not-now needs a non-empty --why.
# - The scorecard window shows roles.<name>.signals = {raised, called, not_now, unanswered}.
COOLDOWN = timedelta(hours=12)


def raise_case(tmp_path, monkeypatch, signals):
    case = signal_case(tmp_path, monkeypatch)
    case.roles["security"]["signals"] = signals
    write_role_snapshot(case.root, case.roles)
    case.pushes = notify_blocked_capture(monkeypatch)
    case.gh = record_gh(monkeypatch)
    return case


def raise_at(case, at):
    from foragent import orch_role_raise
    from foragent.state import StateStore

    case.clock[0] = at
    case.store = StateStore(case.store.root, now=lambda: case.clock[0])
    return orch_role_raise.raise_due(case.store, case.root, at)


def signal_requests(case):
    from foragent.orch_inbox import requests

    return [row for row in requests(case.store) if row.get("role")]


def kinds_snapshot(monkeypatch):
    from foragent import orch_facts

    snapshot = {"kinds": []}
    monkeypatch.setattr(orch_facts, "collect", lambda *args, **kwargs: snapshot)
    return snapshot


def answer(case, request_id, how, why):
    return cli_exit(case.main, ["orch", "signal-answer", request_id, how, "--why", why,
                                "--repo-root", str(case.root)])


def test_a_met_signal_raises_one_gate_request_with_its_numbers_and_never_reaches_the_person(
        tmp_path, monkeypatch):
    case = raise_case(tmp_path, monkeypatch, [{"paths": ["src/payment/**"], "kinds": []}])
    completed(case, 1, finished=NOW - timedelta(hours=1), paths=["src/payment/charge.py", "src/payment/refund.py"])
    raised = raise_at(case, NOW)
    rows = signal_requests(case)
    assert len(raised) == 1 and len(rows) == 1, "One met signal raises exactly one inbox request"
    row = rows[0]
    assert row["from"] == "gate" and row["role"] == "security"
    assert row["signal"] == {"paths": ["src/payment/**"], "kinds": []}, "Name the signal from the roles source"
    assert row["status"] != "done"
    for detail in ("security", "src/payment/**", "2"):
        assert detail in row["text"], f"The request names the role, the signal and its numbers: {detail}"
    assert raise_at(case, NOW + timedelta(minutes=5)) == [], "An open request is never raised twice"
    assert len(signal_requests(case)) == 1
    assert case.pushes == [], "A role signal never pushes to the person"
    assert case.gh == [], "Signals come from local state only"


def test_an_answered_signal_waits_for_the_cooldown_while_it_stays_met(tmp_path, monkeypatch):
    case = raise_case(tmp_path, monkeypatch, [{"paths": ["src/payment/**"], "kinds": []}])
    completed(case, 1, finished=NOW - timedelta(hours=1), paths=["src/payment/charge.py"])
    [first] = raise_at(case, NOW)
    case.clock[0] = NOW + timedelta(minutes=10)
    assert answer(case, first["id"], "not-now", "typo fix only") == 0
    assert raise_at(case, NOW + timedelta(hours=11)) == [], "Still met inside the cooldown: stay quiet"
    again = raise_at(case, NOW + COOLDOWN + timedelta(minutes=5))
    assert len(again) == 1 and again[0]["role"] == "security", "After the default 12-hour cooldown, raise again"


def test_a_signal_that_clears_can_be_raised_again_inside_the_cooldown(tmp_path, monkeypatch):
    snapshot = kinds_snapshot(monkeypatch)
    case = raise_case(tmp_path, monkeypatch, [{"paths": [], "kinds": ["hot_overlap_files"]}])
    snapshot["kinds"] = ["hot_overlap_files"]
    [first] = raise_at(case, NOW)
    case.clock[0] = NOW + timedelta(minutes=10)
    assert answer(case, first["id"], "called", "architect looked at the overlap") == 0
    snapshot["kinds"] = []
    assert raise_at(case, NOW + timedelta(hours=1)) == [], "An unmet signal raises nothing"
    snapshot["kinds"] = ["hot_overlap_files"]
    assert len(raise_at(case, NOW + timedelta(hours=2))) == 1, "A signal that cleared and came back is new"
    assert raise_at(case, NOW + timedelta(hours=2, minutes=5)) == []


def test_answers_are_structured_and_the_scorecard_counts_raised_called_not_now_and_unanswered(
        tmp_path, monkeypatch, capsys):
    import json

    snapshot = kinds_snapshot(monkeypatch)
    kinds = ["hot_overlap_files", "files_over_limit", "repeated_design_holds"]
    case = raise_case(tmp_path, monkeypatch, [{"paths": [], "kinds": [kind]} for kind in kinds])
    snapshot["kinds"] = list(kinds)
    raised = raise_at(case, NOW)
    assert len(raised) == 3
    by_kind = {row["signal"]["kinds"][0]: row["id"] for row in raised}
    case.clock[0] = NOW + timedelta(minutes=10)
    call_event(case, "security", NOW + timedelta(minutes=5))
    assert answer(case, by_kind["hot_overlap_files"], "called", "security reviewed the overlap") == 0
    assert answer(case, by_kind["files_over_limit"], "not-now", "") != 0, "not-now needs a reason"
    assert answer(case, by_kind["files_over_limit"], "not-now", "split is already queued") == 0
    from foragent.orch_inbox import requests
    status = {row["id"]: row["status"] for row in requests(case.store)}
    assert status[by_kind["hot_overlap_files"]] == "done" and status[by_kind["files_over_limit"]] == "done"
    assert status[by_kind["repeated_design_holds"]] != "done"
    capsys.readouterr()
    assert cli_exit(case.main, ["orch", "scorecard", "--json", "--repo-root", str(case.root)]) == 0
    payload = json.loads(capsys.readouterr().out)
    signals = payload["window"]["today"]["roles"]["security"].get("signals")
    assert signals == {"raised": 3, "called": 1, "not_now": 1, "unanswered": 1}, (
        "An unanswered signal stays visible for the retrospective instead of escalating")
    assert case.pushes == []


def test_the_gate_tick_raises_role_signals_once_per_tick(tick_case, capsys, monkeypatch):  # noqa: F811
    from foragent import orch_role_raise

    calls = []
    monkeypatch.setattr(orch_role_raise, "raise_due",
                        lambda store, repo_root, now: calls.append((store.root, now)) or [])
    run_tick(tick_case, capsys)
    run_tick(tick_case, capsys)
    assert len(calls) == 2, "Every gate tick checks role signals once"
    assert all(root == tick_case.store.root for root, _ in calls)
