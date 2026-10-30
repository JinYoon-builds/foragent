"""Unit tests for `fa brief --report` (brief_report)."""

from datetime import UTC, datetime, timedelta

import pytest

from foragent import brief_report
from foragent.orch_events import append_event
from foragent.state import StateStore

NOW = datetime(2026, 10, 27, 12, tzinfo=UTC)


def store_at(tmp_path, clock):
    store = StateStore(tmp_path / "state", now=lambda: clock[0])
    store.root.mkdir(parents=True)
    return store


def merge(store, clock, number, at):
    clock[0] = at - timedelta(minutes=10)
    store.start_run(number, branch=f"foragent/issue-{number}", worktree=store.worktree_path(number), lane="api")
    clock[0] = at
    store.record_merge(number, at)
    store.finish_run(number, outcome="merged")


def test_without_a_note_the_first_line_counts_and_a_merged_hold_appears_once(tmp_path):
    clock = [NOW]
    store = store_at(tmp_path, clock)
    append_event(store, NOW - timedelta(hours=30), 7, "escalate", "#7: escalated to human")
    merge(store, clock, 7, NOW - timedelta(hours=1))
    report = brief_report.collect(store, NOW)
    assert report["judgment"] is None
    assert report["merged"] == []  # Listed once, under holds resolved.
    assert report["resolved"] == [{"issue": 7, "by": "merge", "say": None}]
    lines = brief_report.render(report, "en").splitlines()
    assert lines[0] == "0 working · 0 waiting for you · 0 merged since the last report"
    assert "- Hold resolved #7: merged after the hold" in lines
    assert sum("#7" in line for line in lines) == 1
    assert lines[-1] == "Nothing needs you right now."


@pytest.mark.parametrize("kind", ["escalate", "retries-exceeded", "stalled", "orchestrator-timeout", "parked"])
@pytest.mark.parametrize("back", ["retry", "resolve", "unparked"])
def test_every_hold_kind_pairs_with_every_resolving_kind(tmp_path, kind, back):
    clock = [NOW]
    store = store_at(tmp_path, clock)
    append_event(store, NOW - timedelta(hours=30), 5, kind, f"#5 {kind}")  # A hold may predate the window.
    append_event(store, NOW - timedelta(hours=1), 5, back, f"#5 {back}", say="Back in the queue.")
    assert brief_report.collect(store, NOW)["resolved"] == [{"issue": 5, "by": back, "say": "Back in the queue."}]


@pytest.mark.parametrize("kind", ["crash-restart", "stall-restart", "review", "answer"])
def test_other_kinds_neither_hold_nor_resolve(tmp_path, kind):
    clock = [NOW]
    store = store_at(tmp_path, clock)
    append_event(store, NOW - timedelta(hours=3), 5, kind, f"#5 {kind}")
    append_event(store, NOW - timedelta(hours=2), 6, "escalate", "#6 escalated")
    append_event(store, NOW - timedelta(hours=1), 6, kind, f"#6 {kind}")
    append_event(store, NOW - timedelta(minutes=30), 5, "retry", "#5 retry")
    assert brief_report.collect(store, NOW)["resolved"] == []


def test_the_latest_resolving_event_in_the_window_gives_the_line(tmp_path):
    clock = [NOW]
    store = store_at(tmp_path, clock)
    append_event(store, NOW - timedelta(hours=5), 5, "escalate", "#5 escalated")
    append_event(store, NOW - timedelta(hours=4), 5, "retry", "#5 retry", say="First retry.")
    append_event(store, NOW - timedelta(hours=3), 5, "stalled", "#5 stalled")  # A later hold hides nothing.
    append_event(store, NOW - timedelta(hours=2), 5, "resolve", "#5 resolved")
    report = brief_report.collect(store, NOW)
    assert report["resolved"] == [{"issue": 5, "by": "resolve", "say": None}]
    assert "- Hold resolved #5: back in the queue after the hold" in brief_report.render(report, "en").splitlines()


def test_holds_never_come_from_flag_or_approval_snapshots(tmp_path):
    clock = [NOW]
    store = store_at(tmp_path, clock)
    store.save_flag(6, "retries-exceeded", flagged_at=(NOW - timedelta(hours=5)).isoformat())
    store.save_approval(6, body="b", body_sha="s", acceptance_blobs={}, approved_by="human",
                        approved_at=(NOW - timedelta(hours=2)).isoformat())
    append_event(store, NOW - timedelta(hours=1), 6, "retry", "#6 retry")
    assert brief_report.collect(store, NOW)["resolved"] == []


def test_a_hold_that_started_after_its_retry_is_not_resolved(tmp_path):
    clock = [NOW]
    store = store_at(tmp_path, clock)
    append_event(store, NOW - timedelta(hours=2), 8, "retry", "#8: queued implementation retry")
    append_event(store, NOW - timedelta(hours=1), 8, "escalate", "#8: escalated to human")
    assert brief_report.collect(store, NOW)["resolved"] == []


def test_the_window_starts_at_the_recorded_report_time(tmp_path):
    clock = [NOW]
    store = store_at(tmp_path, clock)
    merge(store, clock, 1, NOW - timedelta(hours=2))
    merge(store, clock, 2, NOW - timedelta(minutes=30))
    assert brief_report.last_report(store, NOW) is None
    brief_report.record_report(store, NOW - timedelta(hours=1))
    assert brief_report.last_report(store, NOW) == NOW - timedelta(hours=1)
    report = brief_report.collect(store, NOW)
    assert [item["issue"] for item in report["merged"]] == [2]
    assert report["since"] == (NOW - timedelta(hours=1)).isoformat()


@pytest.mark.parametrize("saved", ["future", "broken", "naive"])
def test_a_future_or_broken_report_time_falls_back_to_the_last_24_hours(tmp_path, saved):
    clock = [NOW]
    store = store_at(tmp_path, clock)
    merge(store, clock, 1, NOW - timedelta(hours=2))
    path = store.root / brief_report.REPORT_FILE
    if saved == "future":
        brief_report.record_report(store, NOW + timedelta(hours=1))
    else:
        path.write_text('{"reported_at": "2026-10-27T11:00:00"}' if saved == "naive" else "{not json",
                        encoding="utf-8")
    assert brief_report.last_report(store, NOW) is None
    report = brief_report.collect(store, NOW)
    assert report["since"] == (NOW - timedelta(hours=24)).isoformat()
    assert [item["issue"] for item in report["merged"]] == [1]


def test_only_open_needs_human_issues_are_the_persons_turn(tmp_path):
    from foragent.tracker import Issue

    clock = [NOW]
    store = store_at(tmp_path, clock)
    store.refresh_local_issues([Issue(9, "Policy", "", frozenset({"needs-human"}), "open"),
                                Issue(10, "Closed one", "", frozenset({"needs-human"}), "closed"),
                                Issue(11, "Blocked", "", frozenset({"blocked"}), "open")], [], {}, NOW)
    assert brief_report.collect(store, NOW)["person_issues"] == [{"issue": 9, "title": "Policy"}]


def test_a_question_without_a_recommendation_lists_its_choices_only():
    report = {"judgment": None, "merged": [], "resolved": [], "working": 0, "lanes": 0, "now": None,
              "questions": [{"question": "Region?", "options": ["Seoul", "Tokyo"], "recommend": None}],
              "person_issues": []}
    lines = brief_report.render(report, "en").splitlines()
    assert lines[-1] == "- Region? 1. Seoul / 2. Tokyo"


def test_questions_show_every_choice_and_mark_the_recommended_one():
    report = {"judgment": "Decide the region.", "merged": [], "resolved": [], "working": 2, "lanes": 1,
              "now": None, "questions": [{"question": "Region?", "options": ["Seoul", "Tokyo"], "recommend": 2}],
              "person_issues": [{"issue": 9, "title": "Policy"}]}
    lines = brief_report.render(report, "en").splitlines()
    assert lines == ["Decide the region.", "Nothing finished since the last report.",
                     "Now: 2 working · lanes busy: 1", "Your turn:",
                     "- Region? 1. Seoul / 2. Tokyo (recommended)", "- #9 Policy needs your decision"]


def test_welcome_draws_the_big_squirrel_with_or_without_colour():
    plain = brief_report.welcome("ko", colour=False)
    assert "\x1b[" not in plain and "█" in plain
    assert plain.splitlines()[-1].startswith("안녕하세요, Foragent예요.")
    assert len(plain.splitlines()) == 10  # Eight cell rows, a blank line and the greeting.
    assert "\x1b[38;5;" in brief_report.welcome("en", colour=True)


def test_report_json_prints_the_same_sections_and_records_the_time(tmp_path, monkeypatch, capsys):
    import json

    from foragent import cli

    clock = [NOW]
    store = store_at(tmp_path, clock)
    merge(store, clock, 3, NOW - timedelta(hours=1))
    root = tmp_path / "project"
    root.mkdir()
    (root / ".foragent.yml").write_text("repo: owner/repo\nlanguage: en\n", encoding="utf-8")
    monkeypatch.setattr(cli.StateStore, "for_repo", classmethod(lambda cls, repo, **kwargs: store))

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW.astimezone(tz) if tz else NOW.replace(tzinfo=None)

    monkeypatch.setattr(cli, "datetime", Clock)
    monkeypatch.setattr(cli.update_check, "check", lambda config: pytest.fail("--report skips the update check"))
    monkeypatch.setattr(cli.update_check, "cached", lambda config: None)
    assert cli.main(["brief", "--report", "--json", "--repo-root", str(root)]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert {"since", "judgment", "merged", "resolved", "working", "now", "questions", "person_issues"} <= printed.keys()
    assert printed["merged"] == [{"issue": 3, "title": None}]
    assert brief_report.last_report(store, NOW) == NOW


@pytest.mark.parametrize("json_output", [False, True])
def test_report_off_prints_the_briefing_with_the_cached_offer_and_never_calls_github(
        tmp_path, monkeypatch, capsys, json_output):
    import json

    from foragent import cli

    clock = [NOW]
    store = store_at(tmp_path, clock)
    root = tmp_path / "project"
    root.mkdir()
    (root / ".foragent.yml").write_text("repo: owner/repo\nlanguage: en\npresence:\n  report: false\n",
                                        encoding="utf-8")
    monkeypatch.setattr(cli.StateStore, "for_repo", classmethod(lambda cls, repo, **kwargs: store))
    offer = {"current": "0.1.0", "latest": "0.2.0", "command": "fa update"}
    monkeypatch.setattr(cli.update_check, "cached", lambda config: offer)
    monkeypatch.setattr(cli.update_check, "check", lambda config: pytest.fail("--report must not look up a release"))
    monkeypatch.setattr(cli.star, "offer", lambda language: pytest.fail("--report must not run gh auth status"))
    options = ["--json"] if json_output else []
    assert cli.main(["brief", "--report", *options, "--repo-root", str(root)]) == 0
    output = capsys.readouterr().out
    if json_output:
        assert json.loads(output)["update"] == offer
    else:
        assert cli.update_check.offer_line(offer, "en") in output
    assert not (store.root / brief_report.REPORT_FILE).exists()


def test_cached_offer_reads_the_last_check_without_a_lookup_or_a_write(tmp_path, monkeypatch):
    import json

    from foragent import update_check

    path = tmp_path / "update-check.json"
    monkeypatch.setattr(update_check, "_cache_path", lambda: path)
    monkeypatch.setattr(update_check, "lookup", lambda: pytest.fail("cached() must not look up a release"))
    monkeypatch.setattr(update_check, "current_version", lambda: "0.1.0")
    monkeypatch.delenv("FORAGENT_UPDATE_CHECK", raising=False)
    assert update_check.cached(None) is None and not path.exists()
    record = {"checked_at": "2020-01-01T00:00:00+00:00", "latest": "v0.2.0"}
    path.write_text(json.dumps(record), encoding="utf-8")
    assert update_check.cached(None) == {"current": "0.1.0", "latest": "0.2.0", "command": update_check.COMMAND}
    assert json.loads(path.read_text(encoding="utf-8")) == record  # A stale cache stays as it is.
    monkeypatch.setenv("FORAGENT_UPDATE_CHECK", "off")
    assert update_check.cached(None) is None


@pytest.mark.parametrize("json_output", [False, True])
def test_report_relays_the_cached_update_offer_without_a_lookup(tmp_path, monkeypatch, capsys, json_output):
    import json

    from foragent import cli

    store = store_at(tmp_path, [NOW])
    root = tmp_path / "project"
    root.mkdir()
    (root / ".foragent.yml").write_text("repo: owner/repo\nlanguage: en\n", encoding="utf-8")
    monkeypatch.setattr(cli.StateStore, "for_repo", classmethod(lambda cls, repo, **kwargs: store))
    offer = {"current": "0.1.0", "latest": "0.2.0", "command": "fa update"}
    monkeypatch.setattr(cli.update_check, "cached", lambda config: offer)
    monkeypatch.setattr(cli.update_check, "check", lambda config: pytest.fail("--report must not look up a release"))
    options = ["--json"] if json_output else []
    assert cli.main(["brief", "--report", *options, "--repo-root", str(root)]) == 0
    output = capsys.readouterr().out
    if json_output:
        assert json.loads(output)["update"] == offer
    else:
        lines = output.splitlines()
        assert lines[-1] == cli.update_check.offer_line(offer, "en")
        assert sum(line == lines[-1] for line in lines) == 1


def test_every_other_fa_brief_in_the_fa_skill_runs_without_the_update_lookup():
    import re
    from pathlib import Path

    skill = (Path(__file__).resolve().parents[1] / "skills/fa/SKILL.md").read_text(encoding="utf-8")
    commands = re.findall(r"`([^`]*\bfa brief\b[^`]*)`", skill)
    others = [command for command in commands if "--report" not in command]
    assert others, "The skill still reads the briefing facts"
    assert all(command.startswith("FORAGENT_UPDATE_CHECK=off ") for command in others), others
