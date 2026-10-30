"""Acceptance test for lane-m2-explainers. Locked by Foragent; do not edit."""

from dataclasses import replace
from datetime import timedelta
import json

import pytest

from test_acorn_interact import hit_positions
from test_lane_view_d76 import lane_d76_case
from test_local_truth import record_gh
from test_skills_autosync import isolated_home
from test_squirrel_running import plain_frame, single_worker, utf8_terminal, view_offline
from test_view_done_state import cell_count


def explanation(frame):
    lines = plain_frame(frame).splitlines()
    top = next((i for i, line in enumerate(lines) if "╭" in line and "╮" in line), None)
    assert top is not None, "role/lane clicks must explain in the orchestrator's existing eight-row bubble"
    assert "╰" in lines[top + 7]
    # The mouth row has ┤ instead of │ at the left edge.
    body = []
    for line in lines[top + 1:top + 7]:
        left = min(i for i, char in enumerate(line) if char in "│┤")
        body.append(line[left + 1:line.rindex("│")].strip())
    return "\n".join(body).strip()


def role_scene(tmp_path, monkeypatch, language="ko", *, live=True, short=True):
    from foragent import orch_architect, watch

    case = lane_d76_case(tmp_path, monkeypatch)
    case.config = replace(case.config, language=language)
    now = case.store._now()
    brief = "레인 경계를 살펴봐요" if language == "ko" else "checking lane boundaries"
    record = {"pid": 88001, "started_at": (now - timedelta(hours=2)).isoformat(),
              "why": "FULL_TECHNICAL_REASON: parser: thresholds (internal evidence remains verbatim)"}
    if short:
        record["short"] = brief
    case.store.root.mkdir(parents=True, exist_ok=True)
    (case.store.root / "architect.json").write_text(json.dumps(record), encoding="utf-8")
    monkeypatch.setattr(orch_architect, "session_alive", lambda rec: bool(live and rec and rec.get("pid") == 88001))
    snapshot = watch.collect(case.status, case.tracker, case.store, case.config, now)
    return case, snapshot, brief


@pytest.mark.parametrize("language,name,docs,active,never", [
    ("ko", "아키텍트", "문서 담당", "아키텍트가 살펴보고 있어요", "필요하면 오케가 불러요"),
    ("en", "architect", "docs", "architect is looking", "the orchestrator calls it when needed"),
])
def test_roles_are_under_the_big_squirrel_not_lanes_and_show_only_the_short_reason(
    tmp_path, monkeypatch, language, name, docs, active, never,
):
    from foragent.watch import render_frame

    case, snapshot, brief = role_scene(tmp_path, monkeypatch, language)
    layout, state = {}, {"store": case.store}
    frame = render_frame(snapshot, case.config, case.root, width=240, state=state, layout=layout)
    lines = plain_frame(frame).splitlines()
    role_line = next((i for i, line in enumerate(lines) if active in line and brief in line), None)
    assert role_line is not None, "r55 roles need a separate line with the caller-written short reason"
    assert docs in lines[role_line] and never in lines[role_line], "both roles share one line when they fit"
    art_end = next(i for i, line in enumerate(lines) if "╰" in line and "╯" in line)
    lane_start = next(i for i, line in enumerate(lines) if "lane-0" in line or "담당0" in line)
    assert art_end < role_line < lane_start
    hits = hit_positions(layout, frame, 240)
    assert ("role", "architect") in hits and ("role", "docs") in hits
    assert ("squirrel", "architect") not in hits and ("squirrel", "docs") not in hits
    assert "FULL_TECHNICAL_REASON" not in plain_frame(frame), "never derive display copy by splitting full why"


@pytest.mark.parametrize("language,ago", [("ko", "2시간 전에 일했어요"), ("en", "worked 2 hours ago")])
def test_resting_role_click_introduces_who_when_and_the_short_reason(tmp_path, monkeypatch, language, ago):
    from foragent.watch import handle_mouse, render_frame

    case, snapshot, brief = role_scene(tmp_path, monkeypatch, language, live=False)
    state, layout = {"store": case.store}, {}
    frame = render_frame(snapshot, case.config, case.root, width=240, state=state, layout=layout)
    hits = hit_positions(layout, frame, 240)
    assert ("role", "architect") in hits, "the role name must be clickable"
    assert ago in plain_frame(frame)
    handle_mouse("left", *hits[("role", "architect")], layout, case.store, case.config, state=state)
    words = explanation(render_frame(snapshot, case.config, case.root, width=240, state=state))
    assert brief in words and ago in words
    assert ("구조" in words and "제안" in words) if language == "ko" else ("structure" in words and "propos" in words)
    assert len([line for line in words.splitlines() if line]) == 3, "role intro has who, last call, reason on three lines"
    assert "FULL_TECHNICAL_REASON" not in words
    assert case.gh_calls == []


def test_legacy_role_without_short_reason_never_leaks_the_full_why(tmp_path, monkeypatch):
    from foragent.watch import handle_mouse, render_frame

    case, snapshot, _ = role_scene(tmp_path, monkeypatch, short=False)
    state, layout = {"store": case.store}, {}
    frame = render_frame(snapshot, case.config, case.root, width=160, state=state, layout=layout)
    hits = hit_positions(layout, frame, 160)
    assert ("role", "architect") in hits, "legacy role records still get a role line"
    handle_mouse("left", *hits[("role", "architect")], layout, case.store, case.config, state=state)
    opened = render_frame(snapshot, case.config, case.root, width=160, state=state)
    assert "FULL_TECHNICAL_REASON" not in plain_frame(frame + opened)
    assert explanation(opened), "missing short copy must not break the introduction"


def test_role_lines_split_on_narrow_screens_without_wrapping_outside_the_frame(tmp_path, monkeypatch):
    from foragent.watch import render_frame

    case, snapshot, _ = role_scene(tmp_path, monkeypatch)
    frame = plain_frame(render_frame(snapshot, case.config, case.root, width=60))
    lines = frame.splitlines()
    architect = next((i for i, line in enumerate(lines) if "아키텍트" in line), None)
    docs = next((i for i, line in enumerate(lines) if "문서 담당" in line), None)
    assert architect is not None and docs is not None and architect != docs, "roles split into one line each when needed"
    assert all(cell_count(line) <= 60 for line in lines)


@pytest.mark.parametrize("language,owns,finished,ratio,lesson", [
    ("ko", "work/**", "이번 주에 3개", "3개 중 2개는 첫 시도에 통과", "작은 변경부터 확인해요"),
    ("en", "work/**", "3 issues this week", "2 of 3 passed on the first try", "Check the small change first"),
])
def test_lane_squirrel_click_shows_three_plain_sentences_from_local_facts_and_newest_memory(
    tmp_path, monkeypatch, language, owns, finished, ratio, lesson,
):
    from foragent import lanes, watch
    from foragent.state import StateStore
    from foragent.tracker import FakeTracker

    snapshot, config = single_worker()
    config = replace(config, language=language, lanes={"work": {
        "paths": ["work/**"], "agent": "fa-work", "skills": ["SECRET_SKILL"], "executor": "codex"}})
    # Deliberately cross a Sunday: this week means local Monday through now.
    now = snapshot.captured_at.astimezone().replace(hour=18)
    monday = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    clock = [monday - timedelta(days=1)]
    store, tracker = StateStore(tmp_path / "state", now=lambda: clock[0]), FakeTracker()
    for number, lane, when, rounds in ((90, "work", monday - timedelta(hours=1), 1),
                                       (91, "work", monday + timedelta(hours=1), 1),
                                       (92, "work", monday + timedelta(hours=2), 2),
                                       (93, "work", monday + timedelta(hours=3), 1),
                                       (94, "elsewhere", monday + timedelta(hours=4), 1)):
        clock[0] = when - timedelta(minutes=10)
        store.start_run(number, branch=f"foragent/issue-{number}", worktree=store.worktree_path(number), lane=lane)
        tracker.add_issue(number, f"Finished {number}", labels=[f"lane:{lane}"], state="closed")
        for round_number in range(1, rounds + 1):
            event = {"at": (when - timedelta(minutes=rounds - round_number)).isoformat(), "issue": number,
                     "kind": "review", "round": round_number, "verdict": "PASS" if round_number == rounds else "FAIL"}
            with (store.root / "orch-events.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(event) + "\n")
        clock[0] = when
        store.finish_run(number, outcome="merged")
    clock[0] = now
    store.refresh_local_issues(tracker.list_issues("all"), [], config.lanes, now)
    memory = tmp_path / ".claude/agent-memory/fa-work/MEMORY.md"
    memory.parent.mkdir(parents=True)
    memory.write_text(f"# Memory\nOLD_LESSON\n\n{lesson}\n", encoding="utf-8")
    snapshot = watch.collect(snapshot.status, tracker, store, config, now, repo_root=tmp_path)
    calls = record_gh(monkeypatch)
    monkeypatch.setattr(lanes, "talk", lambda *a, **kw: pytest.fail("r55(c) explains; it must not open a talk session"))
    state, layout = {"store": store}, {}
    frame = watch.render_frame(snapshot, config, tmp_path, width=240, state=state, layout=layout)
    hits = hit_positions(layout, frame, 240)
    assert ("squirrel", "work") in hits, "lane ownership is explained by its single squirrel"
    watch.handle_mouse("left", *hits[("squirrel", "work")], layout, store, config, state=state)
    words = explanation(watch.render_frame(snapshot, config, tmp_path, width=240, state=state))
    for expected in (owns, finished, ratio, lesson):
        assert expected in words, expected
    assert len([line for line in words.splitlines() if line]) == 3, "lane intro is three plain sentences"
    assert all(value not in words for value in ("SECRET_SKILL", "codex", "OLD_LESSON", "dream", "꿈"))
    assert calls == []


@pytest.mark.parametrize('seconds,unit,count', [(-1,'seconds',0),(0,'seconds',0),(1,'seconds',1),
    (59,'seconds',59),(60,'minutes',1),(3599,'minutes',59),(3600,'hours',1),
    (86399,'hours',23),(86400,'days',1),(172800,'days',2)])
def test_role_age_parts_match_existing_gates_boundaries(seconds, unit, count):
    from foragent import gates
    from foragent.i18n import text
    from foragent.watch_sections.lane_explainers import _age_parts
    now = single_worker()[0].captured_at
    stamp = (now-timedelta(seconds=seconds)).isoformat()
    assert _age_parts(stamp,now) == (unit,count)
    for language in ('ko','en'):
        assert gates.age(stamp,now,language) == text('gates_age_'+unit,language,count=count)
    assert _age_parts(None,now) is None and _age_parts('invalid',now) is None


@pytest.mark.parametrize('language,names,never', [('ko',('아키텍트','문서 담당'),'필요하면 오케가 불러요'),
    ('en',('architect','docs'),'the orchestrator calls it when needed')])
def test_empty_role_facts_still_draw_both_never_called_role_names(tmp_path,language,names,never):
    from foragent.watch import render_frame
    snapshot, config = single_worker()
    snapshot.role_facts = {}
    layout = {}
    frame = plain_frame(render_frame(snapshot,replace(config,language=language),tmp_path,width=240,layout=layout))
    assert all(name in frame for name in names) and frame.count(never) == 2
    hits = hit_positions(layout,frame,240)
    assert {('role','architect'),('role','docs')} <= hits.keys()


def test_lane_intro_first_line_uses_paths_without_a_lane_prefix(tmp_path):
    from foragent.watch_sections import lane_explainers
    snapshot, config = single_worker()
    snapshot.lane_facts = {'work': {'owned_paths':['work/**','shared/**'], 'week_finished':0,
        'week_first_pass':0,'week_reviewed':0,'memory_latest':None}}
    rows = lane_explainers.explain(('squirrel','work'),snapshot,replace(config,language='en'),tmp_path,
        now=snapshot.captured_at,width=240,state={})
    assert ''.join(value for value, _ in rows[0]) == 'Owns work/**, shared/**.'
