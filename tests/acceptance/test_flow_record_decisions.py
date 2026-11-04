"""Locked acceptance for flow-record-decisions. No policy helper is replaced."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
import json

import pytest

from foragent import orch_spawn, orch_stop, run_merge_turn, scheduler
from foragent.config import Config
from foragent.orch import OrchError
from foragent.runner import run_issue
from foragent.state import StateStore
from foragent.tracker import TrackerError
from test_flow_record_state import records
from test_scheduler import ctx  # noqa: F401
from test_lane_width_by_overlap import _finish, _wide_lane
from test_optimistic_overlap import _issue
from test_state_split import offline_acceptance  # noqa: F401
from test_skills_autosync import isolated_home  # noqa: F401
from test_orch_review_handoff import make_case
from test_ci_mode import mode_case, run, no_external_execution  # noqa: F401


def test_overlap_records_peer_execution_file_and_limit_without_holding_disjoint_control(ctx):
    from dataclasses import replace

    starts = _wide_lane(ctx, 4)
    ctx.config = replace(ctx.config, overlap_per_file=1)
    _issue(ctx, 1, ["src/shared.py"], running=True)
    _issue(ctx, 2, ["src/shared.py"])
    _issue(ctx, 3, ["src/free.py"])
    peer_started = ctx.store.get_run(1).started_at
    for _ in range(2):
        assert scheduler.tick(ctx).skipped is None
    assert starts == [3] and ctx.store.get_run(2) is None
    rows = records(ctx.store, "overlap", 2)
    assert len(rows) == 1
    assert rows[0]["data"]["holds"] == [{
        "file": "src/shared.py", "limit": 1,
        "peers": [{"issue": 1, "run_started_at": peer_started}],
    }]
    _finish(ctx, 1)
    assert scheduler.tick(ctx).skipped is None
    assert starts == [3, 2]
    assert records(ctx.store, "overlap", 2)[-1]["data"]["holds"] == []


def test_spawn_and_stop_skips_are_once_per_reason_and_keep_returns_and_errors(tmp_path, monkeypatch):
    now = datetime(2026, 11, 1, 9, tzinfo=UTC)
    store = StateStore(tmp_path / "state", now=lambda: now)
    config = Config(repo="owner/repo")
    monkeypatch.setenv("FORAGENT_ORCH_SPAWN", "off")
    context = SimpleNamespace(store=store, config=config, now=lambda: now,
                              monotonic=lambda: 0, sleep=lambda _: pytest.fail("unexpected wait"))
    for _ in range(2):
        assert orch_spawn.ensure_orchestrator(tmp_path, store, config, now=now) is None
        with pytest.raises(OrchError):
            orch_stop.stop(context, 41, "fixture stop")
    assert len(records(store, "spawn")) == 1
    assert records(store, "spawn")[0]["data"]["reason"] == "disabled"
    assert len(records(store, "stop", 41)) == 1
    assert records(store, "stop", 41)[0]["data"]["reason"] == "not-running"
    assert store.active_runs() == []


def test_merge_refusal_records_why_position_was_lost_and_still_retries_once(
    git_history, tmp_path, monkeypatch,
):
    case = make_case(git_history, tmp_path, monkeypatch, skill_libraries=[], test_command="true")
    merge, tries = case.tracker.merge_pr, []

    def refuse_once(pr, **kwargs):
        tries.append(pr)
        if len(tries) == 1:
            raise TrackerError("fixture merge refused")
        return merge(pr, **kwargs)

    monkeypatch.setattr(case.tracker, "merge_pr", refuse_once)
    assert run_issue(1, case.ctx) == "merged"
    assert len(tries) == 2 and len(case.tracker.merged_prs) == 1
    rows = records(case.store, "merge-withdrawal", 1)
    assert len(rows) == 1
    assert rows[0]["run_started_at"] == case.store.get_run(1).started_at
    assert rows[0]["data"]["reason"] == "merge-refused"
    assert "fixture merge refused" in rows[0]["data"]["detail"]
    outbound = [comment.body for comments in case.tracker.comments.values() for comment in comments]
    outbound += [pr.body for pr in case.tracker.created_prs]
    push = case.store.root / "push-outbox.jsonl"
    if push.exists():
        outbound.append(push.read_text())
    assert "fixture merge refused" not in json.dumps(outbound), "New local detail must not enter outbound payloads"
    # Withdrawing an already absent seat must not create another transition.
    run_merge_turn.withdraw(case.ctx, run_merge_turn.seat(case.ctx, 1, "main"))
    assert len(records(case.store, "merge-withdrawal", 1)) == 2
    run_merge_turn.withdraw(case.ctx, run_merge_turn.seat(case.ctx, 1, "main"))
    assert len(records(case.store, "merge-withdrawal", 1)) == 2


def test_ci_refusal_changes_are_recorded_through_poll_retry_and_success(
    git_history, tmp_path, monkeypatch,
):
    case = mode_case(git_history, tmp_path, monkeypatch, mode="ci")
    case.ci.damage = "missing"
    pauses = []
    sleep = case.ctx.sleep

    def next_evidence(seconds):
        pauses.append(seconds)
        sleep(seconds)
        # First refusal repeats; then its evidence changes; then CI repairs it.
        case.ci.damage = "missing" if len(pauses) == 1 else "partial" if len(pauses) == 2 else None

    case.ctx.sleep = next_evidence
    assert run(case) == "merged"
    assert len(pauses) == 3 and len(case.tracker.merged_prs) == 1
    rows = [r for r in records(case.store, "ci-evidence", 1) if r["data"]["refused"]]
    assert len(rows) == 2, "Same refusal on a poll is one record; changed rejected evidence is another"
    assert rows[0]["data"]["detail"] != rows[1]["data"]["detail"]
    assert all(r["data"]["reason"] and r["data"]["base_sha"] == case.base_sha
               and r["data"]["head_sha"] == case.tracker.pr_head_sha(2) for r in rows)
    assert records(case.store, "ci-evidence", 1)[-1]["data"]["refused"] is False
