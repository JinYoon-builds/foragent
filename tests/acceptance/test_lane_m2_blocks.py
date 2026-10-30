"""Acceptance test for lane-m2-blocks. Locked by Foragent; do not edit."""

from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from io import StringIO
import re

import pytest

from test_acorn_interact import hit_positions
from test_skills_autosync import isolated_home
from test_squirrel_running import plain_frame, single_worker, utf8_terminal, view_offline
from test_view_done_state import cell_count


def block_scene(language="ko"):
    from foragent.labels import lane_display

    snapshot, config = single_worker()
    names = ("idle-a", "waiting", "ask", "work", "idle-b")
    config = replace(config, language=language, lanes={
        name: {"title": name, "emoji": "", "paths": [f"{name}/**"], "agent": f"fa-{name}"}
        for name in names})
    rows = {}
    template = snapshot.status["lanes"]["work"]
    for name in names:
        rows[name] = {**deepcopy(template), "slug": name, "lane_display": lane_display(config, name, language),
                      "state": "idle", "issue": None, "title": None, "phase": None,
                      "running": [], "held": [], "ready": [], "queued": [], "waiting_for": []}
    for name, number, state in (("work", 101, "working"), ("ask", 201, "waiting-human"),
                                ("waiting", 301, "queued")):
        item = {"number": number, "title": f"Task {number}"}
        rows[name].update(state=state, issue=number, title=item["title"])
        rows[name][{"working": "running", "waiting-human": "held", "queued": "queued"}[state]] = [
            {**item, "state": state}]
    rows["work"]["phase"] = "implementing"
    snapshot.status.update(lanes=rows, summary={"working": 1, "blocked": 0, "waiting_human": 1, "queued": 1},
                           issues={"running": rows["work"]["running"], "needs-human": rows["ask"]["held"],
                                   "ready": rows["waiting"]["queued"]})
    return replace(snapshot, done={name: [] for name in names}, today=4,
                   today_done={"work": 3, "idle-a": 1}), config




@pytest.mark.parametrize("language,steps", [
    ("ko", ("준비", "작성", "로컬", "리뷰", "CI", "머지")),
    ("en", ("prep", "write", "local", "review", "CI", "merge")),
])
def test_each_phase_is_a_word_inside_one_sideways_acorn(tmp_path, language, steps):
    from foragent.watch import render_frame

    snapshot, config = single_worker()
    config = replace(config, language=language)
    for phase, word in zip(("baseline", "implementing", "checking", "reviewing", "ci", "merging"), steps):
        snapshot.status["active_runs"][0]["phase"] = phase
        snapshot.status["lanes"]["work"]["phase"] = phase
        lines = plain_frame(render_frame(snapshot, config, tmp_path, width=160, tick=0)).splitlines()
        assert any(f"◖█#101 {word}▌" in line for line in lines), f"{phase} needs a one-row acorn containing {word!r}"


def test_one_squirrel_per_lane_multiple_acorns_blank_before_next_and_shared_idle_block(tmp_path):
    from foragent.watch import render_frame

    snapshot, config = block_scene()
    row = snapshot.status["lanes"]["work"]
    row["running"].append({"number": 102, "title": "Second worker"})
    snapshot.status["active_runs"].append({**snapshot.status["active_runs"][0], "number": 102})
    row["queued"] = [{"number": n, "title": f"Task {n}"} for n in (103, 104, 105)]
    state, layout = {}, {}
    frame = render_frame(snapshot, config, tmp_path, width=160, state=state, layout=layout)
    lines = plain_frame(frame).splitlines()
    hits = hit_positions(layout, frame, 160)
    assert ("squirrel", "work") in hits, "the single lane squirrel needs a public hit region"
    work_y = hits[("squirrel", "work")][1]
    assert work_y < hits[("squirrel", "ask")][1] < hits[("squirrel", "waiting")][1]
    current = next(i for i, line in enumerate(lines) if "#101 작성" in line)
    assert "#102 작성" in lines[current], "copies share one lane block and one work row when they fit"
    assert "▌  ◖█" in lines[current], "sideways acorns have two-space gaps"
    nxt = next(i for i, line in enumerate(lines) if "다음" in line and "#103" in line)
    assert not lines[nxt - 1][14:].strip(), "leave a blank content row before 다음"
    assert all(f"#{n}" in lines[nxt] for n in (103, 104, 105))
    assert "…" not in lines[nxt], "do not truncate a queue which fits"
    assert any("work" in line and "오늘 3개 머지" in line for line in lines)
    assert sum("쉬고 있어요" in line for line in lines[work_y:]) == 1, "idle lanes share one lying-squirrel block"
    idle_y = next(i for i, line in enumerate(lines) if "idle-a" in line)
    assert "idle-b" in lines[idle_y] and idle_y > hits[("squirrel", "waiting")][1]
    assert any("━" * 20 in line for line in lines), "lane blocks need the r56 heavy separators"


def test_queue_uses_available_rows_then_an_exact_overflow_count(tmp_path):
    from foragent.watch import render_frame

    snapshot, config = single_worker()
    snapshot.status["lanes"]["work"]["queued"] = [
        {"number": n, "title": f"Queued {n}"} for n in range(200, 240)]
    frame = plain_frame(render_frame(snapshot, config, tmp_path, width=80))
    rows = [line for line in frame.splitlines() if "▌" in line and any(f"#{n}" in line for n in range(200, 240))]
    assert len(rows) >= 2, "fill more than one queue row before truncating"
    visible = {int(n) for row in rows for n in re.findall(r"#(2\d\d)(?!\d)", row)}
    assert f"…{40 - len(visible)}" in rows[-1], "overflow must count exactly the acorns omitted"
    assert all(cell_count(line) <= 80 for line in frame.splitlines())


def test_working_stem_rocks_without_moving_the_number_or_changing_text(tmp_path):
    from foragent.watch import render_frame

    snapshot, config = single_worker()
    starts = []
    for tick, stem in enumerate(("◖", "◜", "◖", "◟")):
        lines = plain_frame(render_frame(snapshot, config, tmp_path, width=160, tick=tick)).splitlines()
        line = next((line for line in lines if f"{stem}█#101 작성▌" in line), "")
        assert line, "working acorn rocks only its stem over four animation frames"
        starts.append(cell_count(line[:line.index("#101")]))
    assert len(set(starts)) == 1, "animation must not shift the acorn sideways"


def test_waiting_lane_explains_a_known_dependency(tmp_path):
    from foragent.watch import render_frame

    snapshot, config = block_scene()
    snapshot.status["lanes"]["waiting"]["waiting_for"] = [999]
    frame = plain_frame(render_frame(snapshot, config, tmp_path, width=160))
    assert any("순서를 기다려요" in line and "#999" in line for line in frame.splitlines()), (
        "a known blocker must be named next to the waiting reason")


@pytest.mark.parametrize("language,kind,words", [
    ("ko", "slots", ("자리",)), ("en", "slots", ("slots full",)),
    ("ko", "overlap", ("파일", "work/shared.py")), ("en", "overlap", ("file overlap", "work/shared.py")),
    ("ko", "parked", ("보류",)), ("en", "parked", ("parked",)),
])
def test_waiting_reasons_and_parked_come_from_local_records(
    git_history, tmp_path, monkeypatch, language, kind, words,
):
    from foragent import status, watch
    from foragent.issue import body_hash
    from foragent.orch_events import append_event
    from test_orch_review_handoff import make_case

    case = make_case(git_history, tmp_path, monkeypatch, language=language, skill_libraries=[],
                     test_command="true", max_parallel=1 if kind == "slots" else 5,
                     overlap_per_file=1, lanes={"work": {"paths": ["work/**"]},
                                                "waiting": {"paths": ["waiting/**"]}})
    case.store.update_run(1, lane="work", declared_files=["work/shared.py"], effective_files=["work/shared.py"])
    case.tracker.add_labels(1, ["lane:work"])
    for number, lane in ((1, "work"), (2, "waiting")):
        body = "<!-- foragent:v1 -->\n## Goal\nWork.\n## Out of scope\nNone.\n## Done when\n```sh\ntrue\n```\n## Files\n- work/shared.py\n"
        if number == 2:
            case.tracker.add_issue(number, "Waiting work", body, labels=["approved", "ready", f"lane:{lane}"])
        case.store.save_approval(number, body=body, body_sha=body_hash(body), acceptance_blobs={},
                                 approved_by="human", approved_at=case.clock[0].isoformat())
    case.store.update_run(1, approval_body_sha=body_hash(body))
    if kind == "parked":
        append_event(case.store, case.clock[0], 2, "parked", "#2: parked; why: another priority")
    case.store.refresh_local_issues(case.tracker.list_issues("all"), [], case.config.lanes, case.clock[0])
    snapshot = watch.collect(status.collect(case.store, case.config, case.clock[0]), case.tracker,
                             case.store, case.config, case.clock[0], repo_root=case.root)
    frame = plain_frame(watch.render_frame(snapshot, case.config, case.root, width=240, state={"store": case.store}))
    lines = frame.splitlines()
    if kind == "parked":
        relevant = next((line for line in lines if "#2" in line and "▌" in line), "")
        assert relevant and all(word in relevant for word in words), "a parked issue must say parked on its own acorn"
        assert "순서를 기다려요" not in relevant and "waiting its turn" not in relevant
    else:
        assert any(all(word in line for word in words) for line in lines), (
            f"{kind} must be explained from admission facts, not guessed from issue prose")


def test_waiting_and_shared_sleep_have_distinct_animated_pixel_poses(tmp_path):
    from foragent.watch import hit_test, render_frame

    snapshot, config = block_scene()
    frames = []
    for tick in (0, 1, 2, 3):
        layout = {}
        frame = render_frame(snapshot, config, tmp_path, width=160, tick=tick, layout=layout)
        hits = hit_positions(layout, frame, 160)
        assert ("squirrel", "waiting") in hits, "waiting lane needs its lookout squirrel"
        x, y = hits[("squirrel", "waiting")]
        cells = [(cx, cy) for cy in range(len(frame.splitlines())) for cx in range(14)
                 if hit_test(layout, cx, cy) == ("squirrel", "waiting")]
        lines = plain_frame(frame).splitlines()
        crop = "\n".join(lines[cy][:14] for cy in sorted({cy for _, cy in cells}))
        assert any(glyph in crop for glyph in ("▀", "▄")), "lookout is pixel art, not a text-state glyph"
        frames.append(crop)
    assert len(set(frames)) > 1, "lookout must look around and fill dots beside its head"


def test_shared_idle_squirrel_lies_down_and_its_sleep_marks_move(tmp_path):
    from foragent.watch import render_frame

    snapshot, config = block_scene()
    crops = []
    for tick in range(8):
        frame = plain_frame(render_frame(snapshot, config, tmp_path, width=160, tick=tick))
        lines = frame.splitlines()
        names = next((i for i, line in enumerate(lines) if "idle-a" in line and "idle-b" in line), None)
        assert names is not None, "idle lane names must share the lying squirrel's block"
        # The prototype's shared block is four art rows, with its names on row 3.
        crop = "\n".join(line[:11] for line in lines[names - 2:names + 2])
        assert any(char in crop for char in "▀▄"), "the shared idle block needs the lying pixel squirrel"
        crops.append(crop)
    assert len(set(crops)) > 1, "the lying squirrel's sleep marks must rise"




@pytest.mark.parametrize('phase', ['overlap-wait','test-slot-wait','baseline-wait','executor-wait','limit-wait'])
def test_running_wait_acorn_also_has_a_waiting_sentence_without_fabricating_a_cause(tmp_path,phase):
    from test_lane_m2_contract import draw, bubble
    snapshot, config = single_worker(phase=phase)
    snapshot.waiting_facts = {}
    frame = draw(snapshot,config,tmp_path,width=160)
    assert '#101 대기' in frame and '순서를 기다려요' in frame
    assert '레인 1곳에서 이슈 1개를 작업하고 있어요.' in bubble(frame)


def test_representative_question_state_does_not_recolour_other_running_acorns(tmp_path):
    from test_lane_m2_contract import draw
    snapshot, config = single_worker(state='waiting-human')
    row = snapshot.status['lanes']['work']
    row['running'].append({'number':105,'title':'Second'})
    snapshot.status['active_runs'].append({**snapshot.status['active_runs'][0],'number':105,'phase':'implementing'})
    snapshot.status['issues'] = {}
    frame = draw(snapshot,config,tmp_path,width=160)
    assert '#101 질문' in frame and '#105 작성' in frame and '#105 질문' not in frame
