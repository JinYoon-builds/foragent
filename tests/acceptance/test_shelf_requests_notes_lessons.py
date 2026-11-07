"""Acceptance: the request box, planning notes, lane and role lessons and unordered decisions
sit on the accumulation object (order 2026-11-05 night structure, issue
shelf-requests-notes-lessons).

Everything is observed through public entry points: the gate tick (`scheduler.tick`, what
`fa gate tick` runs), `fa orch inbox|questions|request|request-status|role-decision|roles`,
`fa order note`, `fa brief --json`, `.foragent.yml` loading and the state drafts folder
(`<state>/drafts/<order-id>/plan.json`, the fa-order plan schema with `issues[].decisions`). A PM alarm is one request in
the orchestrator request box from the gate to the PM (from: gate, to: pm, kind: shelf-alarm),
and `fa brief --json` lists the open ones.

No real process starts and no signal is sent: role sessions go to a fake Popen and a fake
process table, issue and dream starts go to a recording launcher, and GitHub is a FakeTracker.
"""

from dataclasses import fields, replace
from datetime import UTC, datetime, timedelta
import json
import re
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from foragent import capacity, executors, scheduler
from foragent.executors import ExecResult
from foragent.state import StateStore
from test_board_status_groups import offline_github  # noqa: F401
from test_orch_review_handoff import bind_cli, make_case, tick_context
from test_order_machine_budget import fake_workloads  # noqa: F401
from test_skills_autosync import cli_exit, isolated_home  # noqa: F401

ARCHITECT_MEMORY = ".claude/agent-memory/fa-architect"
LANE_MEMORY = ".claude/agent-memory/orch"
PERSON_MARK = "Decision: person"


# ---------------------------------------------------------------- fixture and helpers


@pytest.fixture
def shelf(git_history, tmp_path, monkeypatch, fake_workloads, offline_github, capsys):
    """An idle project: two lanes, one role, no open issue, no run in progress."""
    case = make_case(git_history, tmp_path, monkeypatch, skill_libraries=[], test_command="true",
                     language="en", max_parallel=2,
                     lanes={name: {"title": name, "paths": [f"{name}/**"]} for name in ("orch", "view")})
    case.store.finish_run(1, "merged")
    case.tracker.close_issue(1)
    case.tracker.remove_labels(1, ["running", "approved"])
    case.ctx.config = replace(case.ctx.config, roles={"architect": {
        "agent": "fa-architect", "skills": ["fa-architect"], "memory": ARCHITECT_MEMORY + "/",
        "signals": []}})
    case.capsys = capsys
    case.launches, case.starts = [], []
    original = subprocess.Popen

    def popen(argv, *args, **kwargs):
        if not isinstance(argv, (list, tuple)) or "foragent.cli" not in [str(part) for part in argv]:
            return original(argv, *args, **kwargs)
        child = SimpleNamespace(pid=981001 + len(case.launches), argv=[str(part) for part in argv],
                                kwargs=kwargs, returncode=None)
        case.launches.append(child)
        fake_workloads.add(child.pid, cwd=case.root, start_time=float(child.pid))
        return child

    def execute(**kwargs):
        return ExecResult(0, False, "Nothing to change.", kwargs["log_path"])

    monkeypatch.setattr(subprocess, "Popen", popen)
    monkeypatch.setattr(executors, "executor_for", lambda name: SimpleNamespace(run=execute))
    monkeypatch.setattr(scheduler, "machine_load", lambda: ("normal", 0.5))
    monkeypatch.setattr(capacity, "machine_load", lambda: ("normal", 0.5))
    monkeypatch.setattr(capacity, "kernel_pressure", lambda: "normal")
    monkeypatch.setattr(capacity, "machine_root", lambda: case.store.root.parent)
    monkeypatch.setattr("foragent.scheduler.os.kill", lambda pid, signal: None)
    capacity.write_state(case.store, 2, case.clock[0])
    case.main = bind_cli(case, monkeypatch)
    return case


def tick(case, **overrides):
    """One gate tick with a launcher that records issue and dream starts and starts nothing."""
    ctx = tick_context(case, **overrides)

    def launch(number=None, *, kind="issue", lane=None, **extra):
        case.starts.append({"number": number, "kind": kind, "lane": lane, **extra})
        return 88000 + len(case.starts)

    ctx.launcher = launch
    report = scheduler.tick(ctx)
    assert report.skipped is None, report.actions
    return report


def later(case, **delta):
    case.clock[0] += timedelta(**delta)
    case.store = StateStore(case.store.root, now=lambda: case.clock[0])
    case.ctx.store = case.store


def cli_json(case, *argv):
    case.capsys.readouterr()
    assert cli_exit(case.main, [*argv, "--json", "--repo-root", str(case.root)]) == 0, argv
    return json.loads(case.capsys.readouterr().out)


def inbox(case, *, pending=False):
    return cli_json(case, "orch", "inbox", *(["--pending"] if pending else []))


def questions(case):
    return cli_json(case, "orch", "questions", "--all")


def brief(case):
    return cli_json(case, "brief")


def request(case, text, source="orchestrator"):
    before = {row["id"] for row in inbox(case)}
    case.capsys.readouterr()
    assert cli_exit(case.main, ["orch", "request", "--from", source, "--text", text,
                                "--repo-root", str(case.root)]) == 0
    (row,) = [row for row in inbox(case) if row["id"] not in before]
    return row


def set_status(case, request_id, status):
    root = ["--repo-root", str(case.root)]
    extra = ["--answer", "Handled in the planning session"] if status == "done" else []
    case.capsys.readouterr()
    assert cli_exit(case.main, ["orch", "request-status", request_id, status, *extra, *root]) == 0


def is_alarm(row):
    return row.get("from") == "gate" and row.get("to") == "pm" and row.get("kind") == "shelf-alarm"


def all_alarms(case):
    """Every PM alarm record ever written, open or done (done ones may be archived later)."""
    return [row for row in inbox(case) if is_alarm(row)]


def open_alarms(case):
    return [row for row in inbox(case, pending=True) if is_alarm(row)]


def values(node):
    """Every string anywhere in a JSON value."""
    if isinstance(node, dict):
        for value in node.values():
            yield from values(value)
    elif isinstance(node, list):
        for value in node:
            yield from values(value)
    elif isinstance(node, str):
        yield node


def assert_in_brief(case, alarm):
    listed = set(values(brief(case)))
    assert alarm["id"] in listed, "fa brief --json must list the open PM alarm so planning sees it next turn"


def at_wall_clock(case):
    """Move the fixture clock to the real time, so records stamped by either clock agree."""
    case.clock[0] = datetime.now(UTC).replace(microsecond=0)
    case.store = StateStore(case.store.root, now=lambda: case.clock[0])
    case.ctx.store = case.store


def lessons(count, tag, day):
    return "".join(f"- [id:{tag}-{day}-{index}] Opaque lesson {tag} number {index}.\n" for index in range(count))


def write_memory(case, folder, pending, consumed=""):
    path = case.root / folder
    path.mkdir(parents=True, exist_ok=True)
    (path / "MEMORY.md").write_text("# Memory\n- Curated lesson.\n", encoding="utf-8")
    (path / "MEMORY.new.md").write_text(pending, encoding="utf-8")
    (path / "MEMORY.consumed.ids").write_text(consumed, encoding="utf-8")
    files = {f"{folder}/{name}": (path / name).read_text(encoding="utf-8")
             for name in ("MEMORY.md", "MEMORY.new.md", "MEMORY.consumed.ids")}
    # The same lines are on the base branch, committed at the fixture's current time. A tick may
    # have pushed role memory meanwhile, so the next commit follows the origin's current tip.
    from conftest import git_command

    case.history.tip = git_command(case.history.origin, "rev-parse", "refs/heads/main")
    case.history.update(files, at=case.clock[0], message="Lane and role lessons")


def dream_starts(case, lane):
    return [start for start in case.starts if start["kind"] == "dream" and start["lane"] == lane]


def role_starts(case, role):
    """Role sessions started for ``role``: a gate-launched worker naming the role, or a launcher call."""
    started = []
    for child in case.launches:
        argv = child.argv
        if "--role" in argv and argv[argv.index("--role") + 1] == role:
            started.append(child)
    started += [start for start in case.starts if role in {start.get("lane"), start.get("role")}]
    return started


# ---------------------------------------------------------------- request box


def test_request_box_over_twenty_open_requests_raises_one_pm_alarm_listed_in_brief(shelf):
    case = shelf
    tick(case)  # Anything the gate files by itself is counted before the test fills the box.
    present = [row for row in inbox(case, pending=True) if not is_alarm(row)]
    assert len(present) <= 20 and not all_alarms(case), present
    for index in range(20 - len(present)):
        request(case, f"Opaque request Ꮚ{index}")
    later(case, minutes=1)
    tick(case)
    assert all_alarms(case) == [], "Twenty open requests are at the limit, not past it"
    request(case, "Opaque request one past the limit Ꮚ21")
    later(case, minutes=1)
    tick(case)
    alarms = all_alarms(case)
    assert len(alarms) == 1, f"Twenty-one open requests must raise exactly one PM alarm: {alarms}"
    assert alarms[0]["status"] != "done"
    for _ in range(2):
        later(case, minutes=1)
        tick(case)
    assert len(all_alarms(case)) == 1, "While the box stays over its limit no second alarm is raised"
    assert_in_brief(case, alarms[0])


@pytest.mark.parametrize("status", ["new", "processing"])
def test_request_open_past_twenty_four_hours_raises_one_pm_alarm(shelf, status):
    case = shelf
    start = case.clock[0]
    case.clock[0] = start - timedelta(hours=25)
    finished = request(case, "Opaque request finished long ago ᏍD")
    case.clock[0] = start - timedelta(hours=24, minutes=50)
    set_status(case, finished["id"], "done")
    case.clock[0] = start - timedelta(hours=23)
    waiting = request(case, f"Opaque request still {status} Ꮉ{status}")
    if status == "processing":
        set_status(case, waiting["id"], "processing")
    case.clock[0] = start
    later(case, hours=0)
    tick(case)
    assert all_alarms(case) == [], ("A done request is not open, and an open request "
                                    "23 hours old is inside the 24-hour limit")
    later(case, hours=2)  # The open request is now 25 hours old.
    tick(case)
    alarms = all_alarms(case)
    assert len(alarms) == 1, f"A request {status} for more than 24 hours must raise one PM alarm: {alarms}"
    later(case, minutes=5)
    tick(case)
    assert len(all_alarms(case)) == 1, "The same episode must not raise a second alarm"
    assert_in_brief(case, alarms[0])


def test_person_decision_requests_reach_the_question_box_at_most_five_a_day(shelf):
    case = shelf
    tokens = [f"Ꙭ{index}q" for index in range(7)]
    for token in tokens:
        request(case, f"Pick the retention rule {token}.\n{PERSON_MARK}\n")
    request(case, "Opaque ordinary request Ꙩplain for planning only")
    for _ in range(2):
        later(case, minutes=1)
        tick(case)

    def reached():
        found = {}
        for question in questions(case):
            text = json.dumps(question, ensure_ascii=False)
            for token in [*tokens, "Ꙩplain"]:
                if token in text:
                    found.setdefault(token, []).append(question)
        return found

    found = reached()
    assert "Ꙩplain" not in found, "A request not marked as a person decision must not reach the person"
    assert len(found) == 5, f"Exactly five person decisions may reach the person in one day: {sorted(found)}"
    assert all(len(rows) == 1 for rows in found.values()), "Each person decision becomes one question"
    for (question,) in found.values():
        assert question["status"] == "open"
        assert len(question["options"]) >= 2, "A question for the person is multiple choice"
    later(case, hours=25)
    for _ in range(2):
        tick(case)
        later(case, minutes=1)
    found = reached()
    assert set(found) == set(tokens), "The rest reach the person on the next day, within the cap"
    assert all(len(rows) == 1 for rows in found.values()), "No person decision is asked twice"


# ---------------------------------------------------------------- planning notes


def test_notes_waiting_at_a_boundary_past_twenty_four_hours_alarm_the_pm(shelf):
    case = shelf
    at_wall_clock(case)
    case.capsys.readouterr()
    assert cli_exit(case.main, ["order", "note", "--defer", "next-order", "--kind", "idea",
                                "--text", "Opaque waiting idea Ꮻ", "--repo-root", str(case.root)]) == 0
    tick(case)
    later(case, hours=23)
    tick(case)
    assert all_alarms(case) == [], "A note waiting 23 hours is inside its 24-hour limit"
    later(case, hours=2)
    tick(case)
    alarms = all_alarms(case)
    assert len(alarms) == 1, f"A note waiting past 24 hours at its boundary must alarm the PM once: {alarms}"
    later(case, minutes=5)
    tick(case)
    assert len(all_alarms(case)) == 1, "The same episode must not raise a second alarm"
    assert_in_brief(case, alarms[0])


# ---------------------------------------------------------------- lane and role lessons


def test_lane_with_more_than_one_hundred_pending_lessons_starts_its_dream(shelf):
    case = shelf
    # Thirty consumed lines do not count: 100 lessons are pending.
    consumed = "".join(f"done-{index}\n" for index in range(30))
    old = "".join(f"- [id:done-{index}] Already folded lesson {index}.\n" for index in range(30))
    write_memory(case, LANE_MEMORY, old + lessons(100, "orch", "fresh"), consumed)
    tick(case)
    later(case, minutes=1)
    tick(case)
    assert case.starts == [], "One hundred pending lessons are at the limit, not past it"
    write_memory(case, LANE_MEMORY, old + lessons(101, "orch", "fresh"), consumed)
    later(case, minutes=1)
    tick(case)
    assert len(dream_starts(case, "orch")) == 1, ("A lane past 100 pending lessons must start its dream",
                                                  case.starts)
    assert dream_starts(case, "view") == [], "Another lane's dream does not start"


def test_lane_lesson_pending_past_seven_days_starts_its_dream(shelf):
    case = shelf
    at_wall_clock(case)
    day = case.clock[0].date().isoformat()
    write_memory(case, LANE_MEMORY, lessons(1, "orch", day))
    tick(case)
    later(case, days=6)
    tick(case)
    assert case.starts == [], "A lesson pending six days is inside its 7-day limit"
    later(case, days=1, hours=1)
    tick(case)
    assert len(dream_starts(case, "orch")) == 1, ("A lesson pending past 7 days must start the lane's dream",
                                                  case.starts)
    assert dream_starts(case, "view") == []


def test_role_with_more_than_one_hundred_pending_lessons_starts_its_curation(shelf):
    case = shelf
    write_memory(case, ARCHITECT_MEMORY, lessons(100, "architect", "fresh"))
    tick(case)
    later(case, minutes=1)
    tick(case)
    assert role_starts(case, "architect") == [], "One hundred pending role lessons are at the limit"
    write_memory(case, ARCHITECT_MEMORY, lessons(101, "architect", "fresh"))
    later(case, minutes=1)
    tick(case)
    assert len(role_starts(case, "architect")) == 1, (
        "A role past 100 pending lessons must start that role's curation", [c.argv for c in case.launches])
    later(case, minutes=1)
    tick(case)
    assert len(role_starts(case, "architect")) == 1, "A running curation must not be started twice"


def test_role_lesson_pending_past_seven_days_starts_its_curation(shelf):
    case = shelf
    at_wall_clock(case)
    day = case.clock[0].date().isoformat()
    write_memory(case, ARCHITECT_MEMORY, lessons(1, "architect", day))
    tick(case)
    later(case, days=6)
    tick(case)
    assert role_starts(case, "architect") == [], "A role lesson pending six days is inside its limit"
    later(case, days=1, hours=1)
    tick(case)
    assert len(role_starts(case, "architect")) == 1, (
        "A role lesson pending past 7 days must start that role's curation", [c.argv for c in case.launches])


# ---------------------------------------------------------------- decisions with no issue yet


LEDGER = """# Decisions

### D1. 2026-09-01 Already ordered
Linked to issue #40.

### D2. 2026-09-02 Oldest decision with no issue
Opaque decision two.

### D3. 2026-09-03 Second decision with no issue
Opaque decision three.

### D4. 2026-09-04 Newest decision with no issue
Opaque decision four.
"""


def decided(case, *, linked=True):
    path = case.root / "docs/decisions.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(LEDGER, encoding="utf-8")
    if linked:
        case.tracker.add_issue(40, "Ordered work", "Goal.\n<!-- foragent:decisions [\"D1\"] -->\n",
                               labels=["lane:orch"])


def draft_order(case, order_id, decision, files):
    folder = case.store.root / "drafts" / order_id
    folder.mkdir(parents=True, exist_ok=True)
    plan = {"id": order_id, "title": f"Draft for {decision}", "summary": "Drafted earlier",
            "spec_markdown": f"# {decision}\n", "issues": [
                {"key": "one", "title": "One", "goal": f"Carry out {decision}", "out_of_scope": "Other",
                 "done_when": ["true"], "lane": "orch", "size": "s", "decisions": [decision],
                 "files": list(files)}]}
    (folder / "plan.json").write_text(json.dumps(plan), encoding="utf-8")


def drafted(case):
    """Decision id -> the draft order folders (``<state>/drafts/<id>/plan.json``) whose issues name it."""
    found = {}
    for plan_path in sorted((case.store.root / "drafts").glob("*/plan.json")):
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        named = set(plan.get("decisions") or [])
        for item in plan.get("issues") or []:
            named |= set(item.get("decisions") or [])
        for identity in named:
            found.setdefault(identity, []).append(plan_path.parent.name)
    return found


def draft_texts(case):
    return {path.parent.name: path.read_text(encoding="utf-8")
            for path in (case.store.root / "drafts").glob("*/plan.json")}


ORDER = ["D2", "D3", "D4"]  # The ledger's decisions with no issue, oldest first.
CONTENT = {"D2": "Opaque decision two", "D3": "Opaque decision three", "D4": "Opaque decision four"}
TITLE = {"D2": "Oldest decision with no issue", "D3": "Second decision with no issue",
         "D4": "Newest decision with no issue"}


def decision_requests(case):
    """Open requests for the orchestrator that name ledger decisions with no issue yet."""
    rows = []
    for row in inbox(case, pending=True):
        if is_alarm(row):
            continue
        named = re.findall(r"(?<![A-Za-z0-9_])(D[1-4])(?![0-9])", row["text"])
        if named:
            rows.append((row, list(dict.fromkeys(named))))
    return rows


def test_target_concurrency_is_a_project_setting_with_default_twelve(tmp_path):
    from foragent.config import Config, load_config

    names = {field.name for field in fields(Config)}
    assert "target_concurrency" in names, "fa config needs the target concurrency setting"
    (tmp_path / ".foragent.yml").write_text(yaml.safe_dump({"repo": "owner/repo"}))
    assert load_config(tmp_path).target_concurrency == 12
    (tmp_path / ".foragent.yml").write_text(yaml.safe_dump({"repo": "owner/repo", "target_concurrency": 3}))
    assert load_config(tmp_path).target_concurrency == 3


def test_idle_capacity_hands_the_oldest_unordered_decisions_to_the_orchestrator_once(shelf):
    case = shelf
    decided(case)
    tick(case)
    later(case, minutes=1)
    tick(case)
    found = decision_requests(case)
    assert len(found) == 1, f"With no issue running (default target 12) one drafting request is due: {found}"
    row, named = found[0]
    assert row["from"] == "gate", row
    assert "D1" not in named, "A decision that already has an issue is not taken"
    assert named[0] == "D2", f"The oldest decision with no issue comes first: {named}"
    assert named == sorted(named, key=lambda identity: int(identity[1:])), f"Oldest first: {named}"
    assert re.search(r"\bdraft", row["text"], re.IGNORECASE), "The request asks the orchestrator to draft orders"
    later(case, minutes=1)
    tick(case)
    assert len(decision_requests(case)) == 1, "An open drafting request is not filed again"


def test_no_drafting_request_while_running_issues_reach_the_target(shelf):
    case = shelf
    assert "target_concurrency" in {field.name for field in fields(type(case.ctx.config))}, (
        "fa config needs the target concurrency setting")
    decided(case)
    case.tracker.add_issue(31, "Running work", case.tracker.get_issue(1).body, labels=["approved", "running"])
    case.store.start_run(31, branch="foragent/issue-31", worktree=case.store.worktree_path(31))
    case.ctx.config = replace(case.ctx.config, target_concurrency=1)
    tick(case, start_runs=False)
    later(case, minutes=1)
    tick(case, start_runs=False)
    assert decision_requests(case) == [], "One running issue meets a target of one: no idle capacity"
    assert drafted(case) == {}, "No draft order is made while running issues reach the target"
    case.ctx.config = replace(case.ctx.config, target_concurrency=2)
    later(case, minutes=1)
    tick(case, start_runs=False)
    found = decision_requests(case)
    assert len(found) == 1 and found[0][1][0] == "D2", f"Below the target the oldest decision is taken: {found}"
    made = drafted(case)
    assert "D2" in made and len(made["D2"]) == 1, f"Below the target the oldest decision gets one draft order: {made}"


@pytest.mark.parametrize("holder", ["none", "queued", "running"])
def test_draft_files_overlapping_queued_or_running_work_skip_that_decision(shelf, holder):
    case = shelf
    decided(case)
    draft_order(case, "draft-d2", "D2", ["orch/api/handler.py"])
    draft_order(case, "draft-d3", "D3", ["view/screen.py"])
    body = case.tracker.get_issue(1).body + "\n## Files\n- orch/api/**\n"
    if holder == "queued":
        case.tracker.add_issue(30, "Queued work", body, labels=["approved", "ready", "lane:orch"])
    elif holder == "running":
        case.tracker.add_issue(31, "Running work", body, labels=["approved", "running", "lane:orch"])
        case.store.start_run(31, branch="foragent/issue-31", worktree=case.store.worktree_path(31), lane="orch")
    tick(case, start_runs=False)
    later(case, minutes=1)
    tick(case, start_runs=False)
    found = decision_requests(case)
    assert len(found) == 1, f"Idle capacity files one drafting request: {found}"
    named = found[0][1]
    assert "D3" in named, "A draft whose Files touch no queued or running file is taken"
    assert "D4" in named, "A decision without a draft order is not filtered by files"
    if holder == "none":
        assert named[0] == "D2", f"Without a file conflict the oldest decision comes first: {named}"
    else:
        assert "D2" not in named, f"A draft naming files of {holder} work must wait: {named}"
        assert named[0] == "D3", named
    made = drafted(case)
    assert made.get("D4") and len(made["D4"]) == 1, f"A decision without a draft gets exactly one draft order: {made}"
    assert made.get("D2") == ["draft-d2"] and made.get("D3") == ["draft-d3"], (
        f"A decision that already has a draft order is not drafted again: {made}")


def test_idle_capacity_drafts_orders_for_the_oldest_unordered_decisions(shelf):
    """The drafted orders themselves: one draft per taken decision, oldest first, never twice."""
    case = shelf
    decided(case)
    assert drafted(case) == {}
    tick(case)
    later(case, minutes=1)
    tick(case)
    found = drafted(case)
    assert found, "With no issue running (default target 12) draft orders must appear in the drafts folder"
    assert "D1" not in found, "A decision that already has an issue gets no draft order"
    taken = [identity for identity in ORDER if identity in found]
    assert set(found) == set(taken) and taken == ORDER[:len(taken)], (
        f"Draft orders take the oldest decisions with no issue first: {sorted(found)}")
    assert all(len(folders) == 1 for folders in found.values()), f"One draft order per decision: {found}"
    texts = draft_texts(case)
    for identity in taken:
        (folder,) = found[identity]
        assert CONTENT[identity] in texts[folder] or TITLE[identity] in texts[folder], (
            "A draft order carries its decision's ledger text or title", identity, texts[folder])
        assert all(CONTENT[other] not in texts[folder] for other in CONTENT if other not in found), (
            "A draft carries only the decisions it was drafted for")
    folders = set(texts)
    for _ in range(3):
        later(case, minutes=1)
        tick(case)
    again = drafted(case)
    assert {identity: len(rows) for identity, rows in again.items() if identity in found} == {
        identity: 1 for identity in found}, f"A drafted decision is never drafted a second time: {again}"
    assert folders <= set(draft_texts(case)), "Drafted orders stay in the drafts folder until they are published"
