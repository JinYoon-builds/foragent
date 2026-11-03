"""The architect and docs rows and the role and lane introductions, with their exact copy."""

from datetime import timedelta
from pathlib import Path

import pytest

from foragent import watch, watch_explanations
from foragent.config import Config
from foragent.watch_data import Snapshot
from foragent.watch_sections import lane_explainers
from test_watch import NOW

ROOT = Path(".")
TWO_HOURS = (NOW - timedelta(hours=2)).isoformat()
FULL_WHY = "FULL_TECHNICAL_REASON"


def snapshot(role_facts=None, lane_facts=None) -> Snapshot:
    return Snapshot(status={"lanes": {}, "active_runs": []}, done={}, order=None, completed=0, total=0, eta=None,
                    events=[], captured_at=NOW, role_facts=role_facts or {}, lane_facts=lane_facts or {})


def config(language="en", **values) -> Config:
    return Config(language=language, view_glyphs=values.pop("glyphs", "emoji"), **values)


def rows(facts, language="en", width=200, glyphs="emoji"):
    hits = []
    found = lane_explainers.render(snapshot(facts), config(language, glyphs=glyphs), ROOT, width=width, top=5,
                                   now=NOW, hits=hits)
    return found, hits


def plain(line) -> str:
    return "".join(value for value, _ in line)


def said(target, facts=None, lanes=None, language="en", settings=None):
    found = lane_explainers.explain(target, snapshot(facts, lanes), config(language, **(settings or {})), ROOT,
                                    now=NOW, width=80, state=None)
    return None if found is None else [plain(line) for line in found]


ALIVE = {"alive": True, "short": "checking lane boundaries", "last_called_at": TWO_HOURS, "why": FULL_WHY}
RESTING = {"alive": False, "short": "checked lane boundaries", "last_called_at": TWO_HOURS, "why": FULL_WHY}


@pytest.mark.parametrize("language,expected", [
    ("en", "architect is looking · checking lane boundaries   docs · worked 2 hours ago"),
    ("ko", "아키텍트가 살펴보고 있어요 · checking lane boundaries   문서 담당 · 2시간 전에 일했어요"),
])
def test_two_roles_that_fit_share_a_row_three_spaces_apart(language, expected):
    found, _ = rows({"architect": ALIVE, "docs": RESTING}, language)
    assert [plain(line) for line in found] == [expected]


def test_only_an_alive_role_is_coloured():
    found, _ = rows({"architect": ALIVE, "docs": RESTING})
    assert [style for _, style in found[0]] == ["working", "", ""]
    found, _ = rows({"architect": RESTING, "docs": ALIVE})
    assert [style for _, style in found[0]] == ["", "", "working"]


@pytest.mark.parametrize("facts", [{}, {"docs": RESTING}, {"architect": None, "docs": RESTING},
                                   {"architect": "broken", "docs": RESTING}])
def test_a_missing_role_entry_reads_as_never_called_on_its_own(facts):
    found, hits = rows(facts)
    assert plain(found[0]).startswith("architect · the orchestrator calls it when needed   docs · ")
    assert [hit[4] for hit in hits] == [("role", "architect"), ("role", "docs")]


@pytest.mark.parametrize("language,expected", [
    ("en", "docs is looking · No short reason was recorded."),
    ("ko", "문서 담당이 살펴보고 있어요 · 짧은 이유가 남아 있지 않아요."),
])
def test_alive_role_without_short_names_the_absence_never_the_why(language, expected):
    found, _ = rows({"docs": {"alive": True, "short": "  ", "why": FULL_WHY}}, language)
    assert plain(found[0]).endswith(expected)
    assert FULL_WHY not in plain(found[0])


def test_resting_row_is_name_and_age_only():
    found, _ = rows({"architect": RESTING})
    assert plain(found[0]).startswith("architect · worked 2 hours ago   ")
    assert "checked lane boundaries" not in plain(found[0])


def test_role_hits_cover_only_the_name_on_a_shared_row():
    found, hits = rows({"architect": RESTING, "docs": RESTING}, "ko")
    second = watch._cells("아키텍트 · 2시간 전에 일했어요   ")
    assert plain(found[0]) == "아키텍트 · 2시간 전에 일했어요   문서 담당 · 2시간 전에 일했어요"
    assert hits == [(0, 5, watch._cells("아키텍트"), 6, ("role", "architect")),
                    (second, 5, second + watch._cells("문서 담당"), 6, ("role", "docs"))]


@pytest.mark.parametrize("language", ["en", "ko"])
def test_roles_split_exactly_when_the_gap_no_longer_fits(language):
    facts = {"architect": RESTING, "docs": RESTING}
    fitted, _ = rows(facts, language)
    room = watch._cells(plain(fitted[0]))
    assert len(rows(facts, language, width=room)[0]) == 1
    found, hits = rows(facts, language, width=room - 1)
    assert len(found) == 2
    assert [(hit[0], hit[1]) for hit in hits] == [(0, 5), (0, 6)]
    assert all(watch._cells(plain(line)) < room for line in found)


@pytest.mark.parametrize("glyphs,mark", [("emoji", "…"), ("ascii", "...")])
def test_a_long_short_is_cut_first_and_a_wide_prefix_is_cut_too(glyphs, mark):
    long = {"alive": True, "short": "x" * 200, "last_called_at": TWO_HOURS}
    found, _ = rows({"architect": long, "docs": RESTING}, width=40, glyphs=glyphs)
    assert plain(found[0]).startswith("architect is looking · xxx") and plain(found[0]).endswith(mark)
    assert plain(found[1]) == "docs · worked 2 hours ago"
    found, hits = rows({"architect": long}, width=8, glyphs=glyphs)
    assert all(watch._cells(plain(line)) <= 8 and plain(line).endswith(mark) for line in found)
    assert all(hit[2] <= 8 for hit in hits)


def test_control_characters_in_a_short_cannot_break_the_row():
    found, _ = rows({"architect": {"alive": True, "short": "one\ntwo\x1b[31m"}, "docs": RESTING})
    assert len(found) == 1 and "\n" not in plain(found[0]) and "\x1b" not in plain(found[0])


@pytest.mark.parametrize("seconds,expected", [
    (0, ("seconds", 0)), (59, ("seconds", 59)), (60, ("minutes", 1)), (3599, ("minutes", 59)),
    (3600, ("hours", 1)), (86399, ("hours", 23)), (86400, ("days", 1)), (-5, ("seconds", 0)),
])
def test_age_parts_floor_on_the_gate_boundaries(seconds, expected):
    assert lane_explainers._age_parts((NOW - timedelta(seconds=seconds)).isoformat(), NOW) == expected


@pytest.mark.parametrize("value", [None, "", "invalid", "2026-01-01T00:00:00", 12])
def test_age_parts_are_absent_without_an_aware_time(value):
    assert lane_explainers._age_parts(value, NOW) is None
    assert lane_explainers._age_parts(value, NOW.replace(tzinfo=None)) is None


@pytest.mark.parametrize("seconds,english,korean", [
    (0, "worked 0 seconds ago", "0초 전에 일했어요"), (1, "worked 1 second ago", "1초 전에 일했어요"),
    (60, "worked 1 minute ago", "1분 전에 일했어요"), (120, "worked 2 minutes ago", "2분 전에 일했어요"),
    (3600, "worked 1 hour ago", "1시간 전에 일했어요"), (7200, "worked 2 hours ago", "2시간 전에 일했어요"),
    (86400, "worked 1 day ago", "1일 전에 일했어요"), (172800, "worked 2 days ago", "2일 전에 일했어요"),
])
def test_click_age_inflects_each_unit(seconds, english, korean):
    called = {"docs": {"last_called_at": (NOW - timedelta(seconds=seconds)).isoformat(), "short": "s"}}
    assert said(("role", "docs"), called)[1] == english
    assert said(("role", "docs"), called, language="ko")[1] == korean


@pytest.mark.parametrize("language,target,facts,expected", [
    ("en", "architect", {"architect": ALIVE},
     ["I am the architect. I check structure and propose changes.", "looking now", "checking lane boundaries"]),
    ("ko", "architect", {"architect": RESTING},
     ["아키텍트예요. 구조를 살펴보고 바꿀 내용을 제안해요.", "2시간 전에 일했어요", "checked lane boundaries"]),
    ("en", "docs", {},
     ["I am docs. I check documentation and propose updates.", "The orchestrator calls it when needed.",
      "No short reason was recorded."]),
    ("ko", "docs", {},
     ["문서 담당이에요. 문서를 살펴보고 고칠 내용을 제안해요.", "필요하면 오케가 불러요.", "짧은 이유가 남아 있지 않아요."]),
])
def test_role_explanation_is_who_when_and_the_short(language, target, facts, expected):
    assert said(("role", target), facts, language=language) == expected


FULL_WEEK = {"owned_paths": ["work/**", "shared/**"], "week_finished": 3, "week_first_pass": 2,
             "week_reviewed": 3, "memory_latest": "- Check   the small change first"}


@pytest.mark.parametrize("language,expected", [
    ("en", ["Owns work/**, shared/**.", "Finished 3 issues this week. 2 of 3 passed on the first try.",
            "- Check   the small change first"]),
    ("ko", ["work/**, shared/** 경로를 맡아요.", "이번 주에 3개를 마쳤어요. 3개 중 2개는 첫 시도에 통과했어요.",
            "- Check   the small change first"]),
])
def test_lane_explanation_keeps_results_on_one_row_and_memory_literal(language, expected):
    assert said(("squirrel", "work"), lanes={"work": FULL_WEEK}, language=language) == expected


@pytest.mark.parametrize("change,row,expected", [
    ({"week_finished": 1, "week_first_pass": 1, "week_reviewed": 1}, 1,
     "Finished 1 issue this week. 1 of 1 passed on the first try."),
    ({"week_finished": 0, "week_first_pass": 0, "week_reviewed": 0}, 1,
     "Finished 0 issues this week. No review results are recorded yet."),
    ({"week_reviewed": None, "week_first_pass": None}, 1,
     "Finished 3 issues this week. No review results are recorded yet."),
    ({"week_finished": None, "week_first_pass": None, "week_reviewed": None}, 1,
     "This week’s completion history is unavailable. No review results are recorded yet."),
    ({"owned_paths": []}, 0, "No owned paths are recorded yet."),
    ({"memory_latest": None}, 2, "No memory is recorded yet."),
    ({"memory_latest": "  "}, 2, "No memory is recorded yet."),
])
def test_lane_explanation_absences_use_their_exact_copy(change, row, expected):
    found = said(("squirrel", "work"), lanes={"work": {**FULL_WEEK, **change}})
    assert len(found) == 3 and found[row] == expected


def test_lane_without_facts_falls_back_to_its_configured_paths():
    settings = {"lanes": {"work": {"paths": ["work/**"], "skills": ["SECRET"], "executor": "codex"}}}
    found = said(("squirrel", "work"), settings=settings)
    assert found == ["Owns work/**.", "This week’s completion history is unavailable. "
                     "No review results are recorded yet.", "No memory is recorded yet."]
    assert said(("squirrel", "unknown"))[0] == "No owned paths are recorded yet."


@pytest.mark.parametrize("target", [("acorn", 1), ("role", "orchestrator"), ("role", None), ("squirrel", None),
                                    ("orchestrator", None), None])
def test_other_targets_are_not_this_sections(target):
    assert said(target) is None


def test_the_dispatcher_reaches_this_provider():
    found = watch_explanations.explain(("role", "docs"), snapshot(), config(), ROOT, now=NOW, width=80, state=None)
    assert plain(found[0]).startswith("I am docs.") and len(found) == 3


def test_section_declarations():
    assert lane_explainers.ORDER == 11 and lane_explainers.REPLACES == ()
    assert lane_explainers.GAP is False and lane_explainers.GAP_AFTER is False
    assert not hasattr(lane_explainers, "PIN")
