"""Acceptance test for role-signals. Locked by Foragent; do not edit."""

from datetime import datetime, timedelta
import json
import os
import subprocess

import pytest

from test_local_truth import record_gh
from test_order_progress import offline_local_cli
from test_role_source import DEFAULTS, NOW, role_entry, tree_bytes, write_role_snapshot
from test_skills_autosync import cli_exit, isolated_home


@pytest.fixture(autouse=True)
def local_records_only(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Role signal/scorecard observations must not launch processes or send real signals")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(os, "kill", forbidden)
    monkeypatch.setattr(os, "killpg", forbidden)
    # Guard gh's shared boundary as well as process creation; no fake tracker
    # may hide an attempted GitHub read from these observations.
    calls = record_gh(monkeypatch)
    yield
    assert calls == [], "Role signals and role scorecards must make no GitHub calls"


def signal_case(tmp_path, monkeypatch, *, created_at=None):
    from foragent import cli
    from foragent.state import StateStore

    case = offline_local_cli(tmp_path, monkeypatch)
    case.clock = [NOW]
    case.store = StateStore(case.store.root, now=lambda: case.clock[0])
    monkeypatch.setattr(StateStore, "for_repo", classmethod(
        lambda cls, repo, **kw: cls(case.store.root, now=lambda: case.clock[0])))

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return case.clock[0].astimezone(tz) if tz else case.clock[0].replace(tzinfo=None)

    monkeypatch.setattr(cli, "datetime", Clock)
    case.roles = {name: role_entry(name) for name in (*DEFAULTS, "security")}
    case.roles["security"]["signals"] = [{"paths": ["src/payment/**"], "kinds": []}]
    # With no calls yet, idle age starts at role creation. This fixture locks
    # created_at as source metadata, rather than deriving age from file mtimes.
    case.roles["security"]["created_at"] = (created_at or NOW - timedelta(days=1)).isoformat()
    write_role_snapshot(case.root, case.roles)
    case.files_root = tmp_path
    return case


def observe(case, capsys, command):
    capsys.readouterr()
    before = tree_bytes(case.files_root)
    code = cli_exit(case.main, ["orch", command, "--json", "--repo-root", str(case.root)])
    output = capsys.readouterr()
    assert code == 0, f"fa orch {command} --json must succeed from local records: {output.err.strip()}"
    assert tree_bytes(case.files_root) == before, "Observation must not call roles, remove them or write state"
    assert case.gh_calls == [], "Role observations must not call GitHub"
    return json.loads(output.out)


def role_rows(case, capsys):
    # JSON contract assumption: {roles: {name: {signals: [{paths, kinds, met}],
    # removal_proposed: bool}}}. Keep the original selectors beside their truth.
    payload = observe(case, capsys, "roles")
    rows = payload.get("roles") if isinstance(payload, dict) else None
    assert isinstance(rows, dict), "fa orch roles --json must expose a roles mapping keyed by role name"
    assert set(rows) == set(case.roles), "List every configured role, including idle and custom roles"
    for name, entry in case.roles.items():
        signals = rows[name].get("signals")
        assert isinstance(signals, list) and len(signals) == len(entry["signals"]), (
            f"{name}: expose every configured signal, even when unmet")
        for source, measured in zip(entry["signals"], signals):
            assert {key: measured.get(key) for key in ("paths", "kinds")} == source, (
                f"{name}: derive signal checks from the roles source")
            assert type(measured.get("met")) is bool, f"{name}: each signal must expose a boolean met"
        assert type(rows[name].get("removal_proposed")) is bool, f"{name}: expose removal_proposed"
    return rows


def completed(case, number, *, finished, paths, outcome="merged"):
    case.clock[0] = finished - timedelta(minutes=10)
    case.store.start_run(number, branch=f"foragent/issue-{number}", worktree=case.store.worktree_path(number))
    case.store.update_run(number, changed_files=paths)
    case.clock[0] = finished
    case.store.update_run(number, last_progress_at=finished.isoformat())
    if outcome != "running":
        case.store.finish_run(number, outcome=outcome)
    case.clock[0] = NOW


def call_event(case, name, at):
    from foragent.orch_events import append_event

    append_event(case.store, at, None, f"{name}-call", "Measured evidence", role=name, why="Measured evidence")


def proposal_event(case, name, at, request_id, *, accepted):
    from foragent.orch_events import append_event

    # A decision is a structured fact written by the orchestrator. Do not infer
    # acceptance/rejection from proposal prose or request-status=done.
    append_event(case.store, at, None, "role-proposal", "Orchestrator decision",
                 role=name, request_id=request_id, accepted=accepted)


@pytest.mark.parametrize("paths,hours,outcome,expected", (
    (["src/payment/charge.py"], 1, "merged", True),
    (["src/payment/providers/card.py"], 23.5, "merged", True),
    (["src/payment/charge.py"], 24.5, "merged", False),
    (["src/payment/charge.py"], 1, "checks-failed", False),
    (["src/payment/charge.py"], 1, "pr-open", False),
    (["src/payment/charge.py"], 1, "running", False),
    (["src/payments/charge.py"], 1, "merged", False),
    (["src/payment/charge.py"], -1, "merged", False),
    (None, 1, "merged", False),
), ids=("recent", "nested", "stale", "failed", "unmerged-pr", "running", "other-path", "future", "unknown-files"))
def test_paths_signal_uses_only_matching_recent_merged_changes(tmp_path, monkeypatch, capsys,
                                                            paths, hours, outcome, expected):
    case = signal_case(tmp_path, monkeypatch)
    completed(case, 1, finished=NOW - timedelta(hours=hours), paths=paths, outcome=outcome)
    rows = role_rows(case, capsys)
    assert rows["security"]["signals"][0]["met"] is expected, (
        "A path signal requires a matching changed file in a merged run within the last 24 hours")
    assert rows["security"]["removal_proposed"] is False, "A newly created idle role is valid"


def test_path_signals_follow_source_edits_and_expire_without_starting_a_role(tmp_path, monkeypatch, capsys):
    case = signal_case(tmp_path, monkeypatch)
    completed(case, 1, finished=NOW - timedelta(hours=2), paths=["docs/guide.md", "README.ko.md"])
    case.roles["docs"]["signals"] = [{"paths": ["docs/**"], "kinds": []}, {"paths": ["README*"], "kinds": []}]
    write_role_snapshot(case.root, case.roles)
    rows = role_rows(case, capsys)
    assert [signal["met"] for signal in rows["docs"]["signals"]] == [True, True]
    case.roles["docs"]["signals"] = [{"paths": ["manuals/**"], "kinds": []}]
    write_role_snapshot(case.root, case.roles)
    assert role_rows(case, capsys)["docs"]["signals"][0]["met"] is False, "Do not hard-code docs signal paths"
    case.roles["docs"]["signals"] = [{"paths": ["docs/**"], "kinds": []}]
    write_role_snapshot(case.root, case.roles)
    assert role_rows(case, capsys)["docs"]["signals"][0]["met"] is True
    case.clock[0] = NOW + timedelta(hours=25)
    assert role_rows(case, capsys)["docs"]["signals"][0]["met"] is False, "Path signals expire after 24 hours"


@pytest.mark.parametrize("kind", ("repeated_design_holds", "hot_overlap_files", "files_over_limit", "idle_lanes_with_backlog"))
def test_kinds_signals_use_presence_in_the_same_local_facts_view(tmp_path, monkeypatch, capsys, kind):
    from foragent import orch_facts

    case = signal_case(tmp_path, monkeypatch)
    case.roles["security"]["signals"] = [{"paths": [], "kinds": [kind]}]
    write_role_snapshot(case.root, case.roles)
    # The facts producer owns its arithmetic; this consumer gets the same
    # snapshot exposed by fa orch facts. No LLM or prose classifier is involved.
    snapshot = {"kinds": []}
    monkeypatch.setattr(orch_facts, "collect", lambda *args, **kwargs: snapshot)
    assert observe(case, capsys, "facts") == snapshot
    assert role_rows(case, capsys)["security"]["signals"][0]["met"] is False
    snapshot["kinds"] = [kind]
    assert observe(case, capsys, "facts") == snapshot
    assert role_rows(case, capsys)["security"]["signals"][0]["met"] is True
    snapshot["kinds"] = ["another_local_fact"]
    assert role_rows(case, capsys)["security"]["signals"][0]["met"] is False, "Match the configured kind by identity"


def test_scorecard_counts_calls_and_accepted_proposals_per_role_in_window(tmp_path, monkeypatch, capsys):
    case = signal_case(tmp_path, monkeypatch)
    for name, count in (("architect", 2), ("docs", 1), ("security", 3)):
        for index in range(count):
            call_event(case, name, NOW - timedelta(hours=index + 1))
        call_event(case, name, NOW - timedelta(days=8))
        call_event(case, name, NOW + timedelta(hours=1))
    for index, (name, accepted) in enumerate((("architect", True), ("architect", False), ("docs", True),
                                            ("security", True), ("security", True), ("security", False))):
        proposal_event(case, name, NOW - timedelta(hours=1), f"request-{index}",
                       accepted=accepted)
    proposal_event(case, "security", NOW - timedelta(days=8), "old", accepted=True)
    proposal_event(case, "security", NOW + timedelta(hours=1), "future", accepted=True)
    payload = observe(case, capsys, "scorecard")
    today = payload.get("window", {}).get("today", {})
    roles = today.get("roles")
    assert isinstance(roles, dict), "The scorecard window must record per-role calls and accepted_proposals"
    expected = {"orchestrator": (0, 0), "architect": (2, 1), "docs": (1, 1), "security": (3, 2)}
    assert set(roles) == set(expected), "Include custom roles and zero-count idle roles in the scorecard"
    for name, counts in expected.items():
        assert (roles[name].get("calls"), roles[name].get("accepted_proposals")) == counts, (
            f"{name}: count actual calls and accepted proposals, excluding rejections and out-of-window records")
    assert observe(case, capsys, "scorecard") == payload, "Reopening the scorecard must not change its counts"


@pytest.mark.parametrize("created_days,called_days,expected", (
    (15, None, True), (13, None, False), (40, 1, False), (40, 15, True),
), ids=("unused-15-days", "idle-under-14-days", "old-but-recently-called", "last-call-15-days-ago"))
def test_idle_custom_roles_propose_removal_but_defaults_never_do(tmp_path, monkeypatch, capsys,
                                                              created_days, called_days, expected):
    case = signal_case(tmp_path, monkeypatch, created_at=NOW - timedelta(days=created_days))
    if called_days is not None:
        call_event(case, "security", NOW - timedelta(days=called_days))
    for name in DEFAULTS:
        call_event(case, name, NOW - timedelta(days=40))
        for index in range(3):
            proposal_event(case, name, NOW - timedelta(hours=3 - index), f"{name}-{index}", accepted=False)
    rows = role_rows(case, capsys)
    assert rows["security"]["removal_proposed"] is expected, "Use 14 idle days by default, measured from creation or last call"
    assert all(rows[name]["removal_proposed"] is False for name in DEFAULTS), "Never propose removing the three default roles"
    assert "security" in rows and (case.root / ".claude/agents/fa-security.md").is_file(), (
        "A removal proposal is evidence for the orchestrator, not automatic deletion")


@pytest.mark.parametrize("decisions,expected", (
    ((False, False, False), True), ((True, False, False, False), True),
    ((False, False, True), False), ((False, False), False),
), ids=("three-rejected", "last-three-rejected", "latest-accepted", "only-two-rejected"))
def test_only_three_latest_rejected_proposals_propose_custom_role_removal(tmp_path, monkeypatch, capsys,
                                                                       decisions, expected):
    case = signal_case(tmp_path, monkeypatch)
    call_event(case, "security", NOW - timedelta(hours=6))
    # Write out of timestamp order to require recency from event times, not line order.
    for index in reversed(range(len(decisions))):
        proposal_event(case, "security", NOW - timedelta(hours=5, minutes=-index), f"security-{index}",
                       accepted=decisions[index])
    proposal_event(case, "security", NOW + timedelta(hours=1), "future-acceptance", accepted=True)
    proposal_event(case, "docs", NOW - timedelta(minutes=1), "other-role-acceptance", accepted=True)
    rows = role_rows(case, capsys)
    assert rows["security"]["removal_proposed"] is expected, "Use this role's last three decided proposals, not another role's or future decisions"
    assert all(rows[name]["removal_proposed"] is False for name in DEFAULTS)
