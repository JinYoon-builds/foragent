"""Unit tests for the `fa brief` payload and its plain-text rendering."""

from foragent import brief


def sample(**overrides):
    facts = {
        "setup": True, "questions_open": 0,
        "working": [{"lane": "api", "issue": 1, "title": "Implement feature", "phase": "reviewing", "minutes": 2}],
        "queued": [{"lane": "web", "issue": 4, "title": "Login page", "waiting_for": [1]}],
        "to_see": [{"issue": 3, "title": "Choose locale", "kind": "needs-human", "reason": None},
                   {"issue": 5, "title": "Review this PR", "kind": "pr-open", "reason": None}],
        "team": [{"lane": "api", "title": "🧩 Api", "executor": "codex", "fixed_skills": ["house-style"],
                  "used_skills": [], "warnings": []},
                 {"lane": "web", "title": "🧩 Web", "executor": "claude", "fixed_skills": [],
                  "used_skills": [], "warnings": ["no-skills"]}],
        "profile": None, "usage": {},
    }
    return facts | overrides


def test_unconfigured_folder_reports_setup_false(tmp_path):
    assert not brief.configured(tmp_path)
    (tmp_path / ".foragent.yml").write_text("repo: owner/repo\n")
    assert brief.configured(tmp_path)
    assert brief.unconfigured() == {"setup": False}
    assert brief.render(brief.unconfigured(), "en") == "Foragent isn't set up in this folder yet."
    assert brief.render(brief.unconfigured(), "ko") == "이 폴더에는 아직 Foragent가 설정되지 않았어요."


def test_render_lists_each_section_in_english():
    output = brief.render(sample(), "en")
    assert "Working now: 1" in output
    assert "- api #1 Implement feature (reviewing, 2 min)" in output
    assert "- web #4 Login page (waiting for 1)" in output
    assert "#3 Choose locale (needs a decision)" in output
    assert "#5 Review this PR (PR to review)" in output
    assert "fixed skills house-style" in output
    assert output.count("no fixed or used skills") == 1
    assert "Profile: not set" in output and "Usage: nothing recorded yet" in output


def test_render_korean_profile_usage_and_questions():
    usage = {"claude": {"seen_at": "x", "windows": [{"name": "five_hour", "used_percent": 6.0, "resets_at": None}]}}
    output = brief.render(sample(questions_open=2, profile={"kind": "daily-app"}, usage=usage), "ko")
    assert output.splitlines()[0] == "답할 질문 2개"
    assert "지금 하는 일 1개" in output and "(reviewing, 2분)" in output
    assert "프로필: kind=daily-app" in output
    assert "claude 사용량: five_hour 6%" in output


def test_questions_only_payload_keeps_the_short_form(tmp_path):
    from foragent.state import StateStore

    store = StateStore(tmp_path / "state")
    assert brief.collect(store) == {"questions_open": 0}
    assert brief.render(brief.collect(store), "ko") == "알릴 일이 없어요."


def test_every_running_issue_without_a_run_record_is_working(tmp_path):
    from datetime import UTC, datetime

    lane = {"state": "working", "issue": 7, "title": "One", "phase": None,
            "running": [{"number": 7, "title": "One"}, {"number": 8, "title": "Two"}]}
    status = {"active_runs": [], "lanes": {"api": lane}}
    working = brief._working(status, {}, datetime.now(UTC))
    assert [(item["lane"], item["issue"]) for item in working] == [("api", 7), ("api", 8)]


def test_default_configuration_reports_the_main_lane_with_used_skills():
    from types import SimpleNamespace

    from foragent.config import Config

    run = SimpleNamespace(lane="main", skills_used=["tone"])
    store = SimpleNamespace(read_history=lambda: [run], active_runs=lambda: [])
    team = brief._team(store, Config())
    assert [(member["lane"], member["used_skills"], member["warnings"]) for member in team] == [
        ("main", ["tone"], [])]
    configured = brief._team(SimpleNamespace(read_history=lambda: [], active_runs=lambda: []),
                             Config(lanes={"api": {"paths": ["api/**"]}}))
    assert [member["lane"] for member in configured] == ["api"]


def test_briefing_ends_with_the_named_choices_from_the_registry():
    assert brief.render(sample(), "ko").splitlines()[-1] == "무엇을 할까요? [기획 인터뷰] [디자인 인터뷰] [볼 것 처리]"
    assert brief.render(sample(), "en").splitlines()[-1] == (
        "What should we do? [Planning interview] [Design interview] [Handle what to see]")
    step = brief.next_step("ko")
    assert step["question"] == "무엇을 할까요?"
    assert [choice["id"] for choice in step["choices"]] == ["order", "design", "inspect"]
