"""Acceptance tests for queued-startable: the bubble shows only what the pipeline does not,
and startable work is counted positively.

Jin (2026-11-07) read "19개는 순서를 기다려요" while lanes sat idle: the old sentence summed
every queued listing (parked, unapproved, held and dependency-waiting work included).

Statements checked here:
1. Startable work counts only issues that are approved and ready, not blocked, parked or
   held, with no hold reason recorded by the gate this tick (the latest `ready` flow record
   of the issue); a ready issue without approval is counted separately as having no approval.
   Observed in the gate's published dashboard snapshot (`now.json`, written by the same
   writer `fa gate watch` refreshes on opening) as `summary.startable` and the no-approval
   count (`summary.no_approval`; see NO_APPROVAL_KEYS).
2. The bubble's default lines are the finish estimate with the recent merge rate, the
   planning-session request count line and the latest merges, never the raw request text.
3. The bubble is as tall as its lines.
4. The architect and docs line appears only while the architect or docs owner is working.
5. The startable and no-approval counts are published in the gate's dashboard snapshot
   (`now.json`), which `fa gate watch --once` republishes when it opens; the visible chip
   for them belongs to cycle-pipeline and is not checked here.

Everything runs offline: fake tracker, temporary state, no GitHub call on redraw.
"""

from dataclasses import replace
from datetime import timedelta
import json
import os
import re
import time

import pytest

from test_skills_autosync import isolated_home  # noqa: F401
from test_squirrel_running import plain_frame, single_worker, utf8_terminal, view_offline  # noqa: F401

# The no-approval count's key in the published summary; one of these names, exactly one present.
NO_APPROVAL_KEYS = ("no_approval", "unapproved", "unapproved_ready")


@pytest.fixture(autouse=True)
def utc_dates():
    previous = os.environ.get("TZ")
    os.environ["TZ"] = "UTC"
    time.tzset()
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = previous
        time.tzset()


# ---------------------------------------------------------------- startable work (statement 1)

def _queue_case(git_history, tmp_path, monkeypatch, *, every_ready_held=False):
    """One running issue plus a queue whose startable issues are known by construction.

    - #1 runs in api (shared fixture).
    - #2 approved+ready, no ready record: startable (unless every_ready_held, then file-overlap).
    - #3 approved+ready, latest record file-overlap: held by the gate this tick.
    - #4 approved+ready, an older record says it started, the newest says slots-full: held.
    - #5 approved+ready+blocked: blocked.
    - #6 approved+ready, parked with `fa orch park`: parked.
    - #7 approved+ready+needs-human: held for a person.
    - #8 approved, not ready (waits for a dependency): neither startable nor without approval.
    - #9, #10 ready without approval: counted as having no approval, never startable.
    """
    from test_orch_review_handoff import bind_cli, make_case

    lanes = {"api": {"paths": ["api/**"], "title": "Quokka", "emoji": "🧩"},
             "web": {"paths": ["web/**"], "title": "Pelican", "emoji": "🧩"}}
    case = make_case(git_history, tmp_path, monkeypatch, lanes=lanes, language="en")
    main = bind_cli(case, monkeypatch)
    case.tracker.add_labels(1, ["lane:api"])
    case.store.update_run(1, lane="api", phase="implementing")
    add = case.tracker.add_issue
    add(2, "Startable two", labels=["approved", "ready", "lane:web"])
    add(3, "Overlap three", labels=["approved", "ready", "lane:api"])
    add(4, "Slots four", labels=["approved", "ready", "lane:web"])
    add(5, "Blocked five", labels=["approved", "ready", "blocked", "lane:api"])
    add(6, "Parked six", labels=["approved", "ready", "lane:web"])
    add(7, "Asks seven", labels=["approved", "ready", "needs-human", "lane:api"])
    add(8, "Dependent eight", labels=["approved", "lane:web"])
    add(9, "Unapproved nine", labels=["ready", "lane:api"])
    add(10, "Unapproved ten", labels=["ready", "lane:web"])
    # Record the issues locally (the gate's issue refresh) without a tick deciding anything.
    assert main(["gate", "status", "--live", "--json", "--repo-root", str(case.root)]) == 0
    assert main(["orch", "park", "6", "--why", "Wait for the design", "--repo-root", str(case.root)]) == 0
    # The gate's per-issue outcomes of this tick, written with the gate's own flow writer.
    record = case.store.record_flow
    record("ready", issue=3, run_started_at=None, reason="file-overlap", outcome="waiting")
    record("ready", issue=4, run_started_at="2000-01-01T00:00:00+00:00", reason=None, outcome="started")
    record("ready", issue=4, run_started_at=None, reason="slots-full", outcome="waiting")
    if every_ready_held:
        record("ready", issue=2, run_started_at=None, reason="file-overlap", outcome="waiting")
    return case, main


def _published_summary(case, main, capsys):
    """Open the dashboard once (it republishes now.json) and read the published summary."""
    capsys.readouterr()
    assert main(["gate", "watch", "--once", "--repo-root", str(case.root)]) == 0
    screen = plain_frame(capsys.readouterr().out)
    path = case.store.root / "now.json"
    assert path.exists(), "the gate publishes the dashboard snapshot the view reads"
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload.get("summary") or {}, screen


def _no_approval(summary):
    present = [key for key in NO_APPROVAL_KEYS if key in summary]
    assert len(present) == 1, (
        f"the published summary must carry one no-approval count (one of {NO_APPROVAL_KEYS}), "
        f"found keys {sorted(summary)}")
    return summary[present[0]]


def test_startable_counts_only_approved_ready_unheld_issues_and_no_approval_separately(
    git_history, tmp_path, monkeypatch, capsys,
):
    case, main = _queue_case(git_history, tmp_path, monkeypatch)
    capsys.readouterr()
    assert main(["gate", "status", "--json", "--repo-root", str(case.root)]) == 0
    queued = json.loads(capsys.readouterr().out)["summary"]["queued"]
    assert queued >= 6, f"fixture guard: many queued issues besides the one startable, got {queued}"
    summary, _ = _published_summary(case, main, capsys)
    assert "startable" in summary, f"the published summary must count startable work: {sorted(summary)}"
    assert summary["startable"] == 1, (
        "only #2 is approved, ready, unheld and without a hold reason this tick; blocked, parked, "
        "needs-human, file-overlap, slots-full, approved-but-not-ready and unapproved issues never "
        f"count, found {summary['startable']} (queued {queued})")
    assert _no_approval(summary) == 2, "#9 and #10 are ready without approval, counted separately"


def test_startable_is_zero_when_every_approved_ready_issue_has_a_hold_reason(
    git_history, tmp_path, monkeypatch, capsys,
):
    """The live 14:23 case: a subtraction of named holds showed 7 while the true count was 0."""
    case, main = _queue_case(git_history, tmp_path, monkeypatch, every_ready_held=True)
    summary, _ = _published_summary(case, main, capsys)
    assert "startable" in summary, f"the published summary must count startable work: {sorted(summary)}"
    assert summary["startable"] == 0, (
        "every approved and ready issue has a hold reason this tick, so nothing is startable even "
        f"though unapproved and not-ready issues name no hold; found {summary['startable']}")
    assert _no_approval(summary) == 2


def test_opening_the_dashboard_republishes_both_counts_from_this_ticks_records(
    git_history, tmp_path, monkeypatch, capsys,
):
    """Statement 5: each `fa gate watch --once` republishes now.json from the newest records."""
    case, main = _queue_case(git_history, tmp_path, monkeypatch)
    first, _ = _published_summary(case, main, capsys)
    assert first.get("startable") == 1 and _no_approval(first) == 2, first
    # This tick the gate holds #2 behind a file overlap and #9 gets its approval.
    case.store.record_flow("ready", issue=2, run_started_at=None, reason="file-overlap", outcome="waiting")
    case.tracker.add_labels(9, ["approved"])
    assert main(["gate", "status", "--live", "--json", "--repo-root", str(case.root)]) == 0
    case.store.record_flow("ready", issue=9, run_started_at=None, reason="file-overlap", outcome="waiting")
    second, _ = _published_summary(case, main, capsys)
    assert second.get("startable") == 0, (
        f"opening the dashboard again republishes the count from this tick's records: {second}")
    assert _no_approval(second) == 1, f"#9 is approved now; only #10 lacks approval: {second}"


# ---------------------------------------------------------------- the bubble (statements 2, 3)

REMOVED = {  # Lines that repeat the pipeline or the header: never a default line any more.
    "en": ("Working on", "wait their turn", "waits its turn", "Nothing to do now",
           "question to answer", "questions to answer", "Click an acorn"),
    "ko": ("작업하고 있어요", "순서를 기다려요", "지금은 할 일이 없어요", "답할 질문이",
           "도토리를 누르면"),
}
RAW_REQUEST = "Plan the login order"


def _box(frame):
    """(body rows, plain body texts) of the speech bubble, whatever its height."""
    lines = plain_frame(frame).splitlines()
    top = next((i for i, line in enumerate(lines) if "╭" in line and "╮" in line), None)
    assert top is not None, f"the frame needs the orchestrator's speech bubble:\n{frame}"
    bottom = next((i for i in range(top + 1, len(lines)) if "╰" in lines[i] and "╯" in lines[i]), None)
    assert bottom is not None, f"the bubble needs its bottom border:\n{frame}"
    body = []
    for line in lines[top + 1:bottom]:
        left = min(i for i, ch in enumerate(line) if ch in "│┤")
        body.append(line[left + 1:line.rindex("│")].strip())
    return body


def _requests():
    return [
        {"id": 1, "status": "processing", "from": "planning", "text": RAW_REQUEST, "created_at": "2026-01-01T00:00:00+00:00"},
        {"id": 2, "status": "processing", "from": "planning", "text": "Tidy the readme order", "created_at": "2026-01-01T00:01:00+00:00"},
        {"id": 3, "status": "done", "from": "planning", "text": "An old finished request", "created_at": "2026-01-01T00:02:00+00:00"},
        {"id": 4, "status": "processing", "from": "architect", "role": "architect",
         "text": "Decision: orchestrator\nSplit watch_layout", "created_at": "2026-01-01T00:03:00+00:00"},
    ]


def _busy_snapshot(language, *, requests=True, merges=True, eta=True):
    """A worker, two queued issues, a person's question, merges, an estimate and requests."""
    from foragent.tracker import Issue

    snapshot, config = single_worker()
    config = replace(config, language=language)
    snapshot.status["lanes"]["work"].update(
        queued=[{"number": n, "title": f"Next {n}"} for n in (102, 103)],
        held=[{"number": 104, "title": "Choose a name", "state": "waiting-human"}])
    snapshot.status["issues"] = {"needs-human": [{"number": 104, "title": "Choose a name"}],
                                 "ready": [{"number": n, "title": f"Next {n}"} for n in (102, 103)]}
    snapshot.status["summary"].update(queued=2, waiting_human=1)
    snapshot.heartbeats = {"orch": 0}  # A fresh orchestrator: no warning line.
    if merges:
        snapshot.done = {"work": [Issue(n, f"Merged {n}", "", frozenset(), "closed") for n in (41, 42, 43)]}
        snapshot.merged_at = {n: snapshot.captured_at - timedelta(minutes=45 - n) for n in (41, 42, 43)}
        snapshot.today = 3
    else:
        snapshot.done, snapshot.merged_at, snapshot.today = {"work": []}, {}, 0
    snapshot.recent = 3 if merges else 0
    if eta:
        snapshot.eta = snapshot.captured_at + timedelta(hours=2)
        snapshot.eta_range = (snapshot.eta, snapshot.captured_at + timedelta(hours=4))
    else:
        snapshot.eta, snapshot.eta_range = None, None
    snapshot.requests = _requests() if requests else []
    return snapshot, config


ETA = {"en": "If this pace holds, all done around", "ko": "이대로면"}
PACE = {"en": "Merged 3 issues in the last 3 hours.", "ko": "최근 3시간에 3개를 머지했어요."}
MERGES = {"en": "Just merged #43, #42, #41.", "ko": "방금 #43, #42, #41를 머지했어요."}


def _request_line(rows, language):
    if language == "ko":
        return [row for row in rows if "기획 세션 요청" in row]
    return [row for row in rows if re.search(r"planning", row, re.I) and re.search(r"request", row, re.I)]


@pytest.mark.parametrize("language", ["en", "ko"])
def test_default_bubble_is_estimate_requests_count_and_merges_only(tmp_path, language):
    from foragent.watch import render_frame

    snapshot, config = _busy_snapshot(language)
    rows = _box(render_frame(snapshot, config, tmp_path, width=240))
    text = "\n".join(rows)
    for phrase in REMOVED[language]:
        assert phrase not in text, f"{phrase!r} repeats the pipeline and must leave the bubble:\n{text}"
    assert RAW_REQUEST not in text and "Tidy the readme order" not in text, (
        f"the bubble never shows a request's raw text:\n{text}")
    assert "Planning request:" not in text and "기획 세션 요청:" not in text, text
    filled = [row for row in rows if row]
    assert len(filled) == 3, f"three default lines (estimate+pace, requests count, merges):\n{text}"
    estimate, request, merged = filled
    assert estimate.startswith(ETA[language]) and PACE[language] in estimate, (
        f"the first line is the finish estimate with the recent merge rate:\n{text}")
    assert _request_line([request], language) == [request], f"the second line counts planning requests:\n{text}"
    numbers = re.findall(r"\d+", request)
    assert numbers == ["2"], (
        "two planning requests are open (a finished one and an architect proposal do not count), "
        f"and the line states only that count: {request!r}")
    if language == "ko":
        assert "기획 세션 요청 2개" in request and "누르면 목록" in request, request
    assert merged == MERGES[language], f"the last line names the latest merges:\n{text}"


@pytest.mark.parametrize("language", ["en", "ko"])
def test_dashboard_shows_the_request_count_never_its_text(git_history, tmp_path, monkeypatch, capsys, language):
    """The live path: requests through `fa orch request`, a tick publishes, `fa gate watch --once` prints."""
    from test_dash_request_line import dashboard_case, draw, send

    case, main = dashboard_case(git_history, tmp_path, monkeypatch, language)
    send(case, main, capsys, "planning", RAW_REQUEST)
    send(case, main, capsys, "planning", "Tidy the readme order")
    send(case, main, capsys, "architect", "Decision: orchestrator\nSplit watch_layout into smaller modules")
    frame = draw(case, main, capsys, ["gate", "watch"])
    rows = _box(frame)
    text = "\n".join(rows)
    assert RAW_REQUEST not in frame and "Tidy the readme order" not in frame, (
        f"the dashboard never shows a request's raw text:\n{frame}")
    assert "Planning request:" not in frame and "기획 세션 요청:" not in frame, frame
    lines = _request_line(rows, language)
    assert len(lines) == 1, f"one planning-request count line in the bubble:\n{text}"
    assert re.findall(r"\d+", lines[0]) == ["2"], f"two planning requests are open: {lines[0]!r}"
    for phrase in REMOVED[language]:
        assert phrase not in text, f"{phrase!r} repeats the pipeline and must leave the bubble:\n{text}"


@pytest.mark.parametrize("language", ["en", "ko"])
def test_bubble_is_as_tall_as_its_lines(tmp_path, language):
    from foragent.watch import render_frame

    # Wide: the squirrel stands beside the bubble; three short lines make three body rows.
    snapshot, config = _busy_snapshot(language)
    rows = _box(render_frame(snapshot, config, tmp_path, width=240))
    assert len(rows) == 3 and all(rows), f"three lines need exactly three body rows, no blank ones: {rows}"
    # Narrow (squirrel above, no mouth): one line, one body row.
    snapshot, config = _busy_snapshot(language, requests=False, eta=False)
    snapshot.recent = 0  # No estimate and no rate: the merges line alone.
    rows = _box(render_frame(snapshot, config, tmp_path, width=70))
    assert rows == [MERGES[language]], f"one line needs exactly one body row: {rows}"


# ---------------------------------------------------------------- the roles line (statement 4)

ROLE_NAMES = {"en": ("architect", "docs"), "ko": ("아키텍트", "문서 담당")}


def _role_lines(frame, language):
    return [line for line in plain_frame(frame).splitlines()
            if any(name in line for name in ROLE_NAMES[language])]


@pytest.mark.parametrize("language", ["en", "ko"])
def test_roles_line_shows_only_while_a_role_works(tmp_path, language):
    from foragent.watch import render_frame

    snapshot, config = _busy_snapshot(language)
    called = (snapshot.captured_at - timedelta(hours=2)).isoformat()
    for resting in ({}, {"architect": {"alive": False, "last_called_at": called},
                         "docs": {"alive": False, "last_called_at": called}}):
        layout = {}
        frame = render_frame(replace(snapshot, role_facts=resting), config, tmp_path, width=160, layout=layout)
        assert _role_lines(frame, language) == [], (
            f"no role works, so the architect/docs line is not drawn:\n{plain_frame(frame)}")
        assert not [hit for hit in layout.get("hits", []) if hit[-1][0] == "role"], "no role target either"
    for role, index in (("architect", 0), ("docs", 1)):
        other = "docs" if role == "architect" else "architect"
        facts = {role: {"alive": True, "short": "checks the split", "last_called_at": called},
                 other: {"alive": False, "last_called_at": called}}
        frame = render_frame(replace(snapshot, role_facts=facts), config, tmp_path, width=160)
        lines = _role_lines(frame, language)
        assert len(lines) == 1 and ROLE_NAMES[language][index] in lines[0], (
            f"while {role} works, one roles line names it:\n{plain_frame(frame)}")


@pytest.mark.parametrize("language", ["en", "ko"])
def test_dashboard_without_working_roles_draws_no_roles_line(git_history, tmp_path, monkeypatch, capsys, language):
    from test_dash_request_line import dashboard_case, draw

    case, main = dashboard_case(git_history, tmp_path, monkeypatch, language)
    frame = draw(case, main, capsys, ["gate", "watch"])
    assert _role_lines(frame, language) == [], (
        f"no architect or docs session works, so the dashboard has no roles line:\n{frame}")
