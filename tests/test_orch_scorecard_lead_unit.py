"""Unit tests for the five-stage lead time and for the callers that build the scorecard."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "acceptance"))

from foragent import orch_scorecard_lead as lead  # noqa: E402
from test_scorecard_flow import flow  # noqa: E402,F401
from test_skills_autosync import cli_exit, isolated_home  # noqa: E402,F401


def test_decision_dates_prefer_the_heading_date_then_the_section(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "decisions.md").write_text(
        "# Decisions\n\n## 2026-10-25\n\n### D7. Own date (2026-10-24, Jin)\n- x\n"
        "### D8. Section date\n- y\n\n## Undated\n\n### D9. Nothing\n", encoding="utf-8")
    assert lead.decision_dates(tmp_path) == {"D7": datetime(2026, 10, 24, tzinfo=UTC),
                                             "D8": datetime(2026, 10, 25, tzinfo=UTC)}
    assert lead.decision_dates(None) == {}
    assert lead.decision_dates(tmp_path / "missing") == {}


def _medians(**values):
    return {"medians": {stage: values.get(stage.replace("->", "_")) for stage in lead.STAGES}}


def test_only_the_stage_that_grew_most_is_worse_and_unknown_sides_are_skipped():
    today = _medians(ordered_prepared=40, prepared_published=80, published_started=500)
    before = _medians(ordered_prepared=30, prepared_published=20)
    assert lead.worse_names(today, before) == ["lead_time.prepared->published"]
    assert lead.worse_names(before, today) == []


def test_previous_window_is_the_median_of_daily_medians():
    days = [{"medians": dict.fromkeys(lead.STAGES), "issues": [{"issue": 1}]},
            {**_medians(ordered_prepared=10), "issues": []},
            {**_medians(ordered_prepared=30), "issues": [{"issue": 2}]}]
    result = lead.previous(days)
    assert result["medians"]["ordered->prepared"] == 20
    assert result["medians"]["prepared->published"] is None
    assert [entry["issue"] for entry in result["issues"]] == [1, 2]


def test_an_issue_without_an_order_record_has_no_entry(tmp_path):
    store = SimpleNamespace(root=tmp_path)
    moment = datetime(2026, 11, 1, tzinfo=UTC)
    execution = {"started": moment, "merged_at": moment}
    assert lead.entry(store, {"number": 1, "body": "no marker"}, execution, {}) is None
    marked = {"number": 1, "body": "<!-- foragent:plan-child id=gone key=issue-1 -->"}
    assert lead.entry(store, marked, execution, {}) is None


def _spy_card(monkeypatch):
    from foragent import orch_scorecard

    seen = []

    def card(store, config, repo_root, days=7, *, local_only=False):
        seen.append((repo_root, local_only))
        return {"worse": []}

    monkeypatch.setattr(orch_scorecard, "card", card)
    monkeypatch.setattr(orch_scorecard, "worst_line", lambda card, language: None)
    return seen


def test_brief_report_scorecard_line_passes_its_checkout(monkeypatch, tmp_path):
    from foragent import brief_report
    from foragent.config import Config

    seen = _spy_card(monkeypatch)
    brief_report.scorecard_line(SimpleNamespace(), Config(), tmp_path)
    assert seen == [(tmp_path, True)]


def test_brief_command_gives_its_root_to_every_scorecard_reader(flow, capsys, monkeypatch):
    """`fa brief --json` and `fa brief --report` build the full card with the checkout they run in."""
    from foragent import orch_scorecard

    seen, real = [], orch_scorecard.card

    def card(store, config, repo_root, days=7, *, local_only=False):
        seen.append((repo_root, local_only))
        return real(store, config, repo_root, days, local_only=local_only)

    monkeypatch.setattr(orch_scorecard, "card", card)
    for argv in (["brief", "--json"], ["brief", "--report"]):
        capsys.readouterr()
        assert cli_exit(flow.main, [*argv, "--repo-root", str(flow.root)]) == 0, capsys.readouterr().err
    assert seen == [(flow.root.resolve(), True), (flow.root.resolve(), True)]
