"""Acceptance tests for flow-stall-signals (#2088): the gate tells the orchestrator when the flow stalls.

Only public entry points are driven: `fa gate tick --no-start` raises, `fa orch inbox --json`
reads what the orchestrator receives, `fa orch signal-answer` answers, `fa orch request`,
`fa orch request-status`, `fa orch park` and `fa orch unpark` make the records. Issues live in
the offline FakeTracker, which the tick reads like GitHub; no process is started or signalled.

What counts as "the signal": an inbox request written by the gate (``from: gate``). Each fixture
has exactly one stall condition, so every gate request it raises is that stall's signal. The
signal's evidence is its ``text`` and ``facts`` (plus ``signal``); field names are the lane's
choice, so the checks search those values for lane names, issue and request ids and counts.

How "its end is recorded once" is observed: the existing role-signal path records an end as a
``role-signal-clear`` event in orch-events.jsonl; any journal event whose kind names a clear or
an end of this signal counts, and exactly one must appear per ended episode.

Break-it: firing before 30 minutes, firing on approved-but-unstartable work, counting issues
without Done when or closed ones, naming a busy lane, repeating the signal while the stall lasts,
never signalling, or recording the end twice each fail a check below.
"""

from datetime import UTC, datetime, timedelta
import json
import re

import pytest
import yaml

from test_skills_autosync import cli_exit, isolated_home  # noqa: F401
from test_tick_failure_alert import inbox, run_tick, tick_case  # noqa: F401


BODY = ("<!-- foragent:v1 -->\n## Goal\nDo the work.\n## Out of scope\nOther work.\n"
        "## Done when\n```sh\ntrue\n```\n")
NO_DONE_WHEN = "<!-- foragent:v1 -->\n## Goal\nAn idea, not ready.\n## Out of scope\nOther work.\n"
# This repository's own roles block (.foragent.yml): the orchestrator lists no signals.
REPO_ROLES = {
    "orchestrator": {"agent": "fa-orchestrator", "skills": ["fa-orchestrator"],
                     "memory": ".claude/agent-memory/fa-orchestrator/", "signals": [],
                     "created_at": "2026-10-27T07:58:05+00:00"},
    "architect": {"agent": "fa-architect", "skills": ["fa-architect"],
                  "memory": ".claude/agent-memory/fa-architect/",
                  "signals": [{"paths": [], "kinds": [kind]} for kind in (
                      "repeated_design_holds", "hot_overlap_files", "files_over_limit",
                      "idle_lanes_with_backlog", "lane_concentration")],
                  "created_at": "2026-10-27T07:58:05+00:00"},
    "docs": {"agent": "fa-docs", "skills": ["fa-docs"], "memory": ".claude/agent-memory/fa-docs/",
             "signals": [{"paths": ["docs/**", "README*"], "kinds": []}], "scope": ["docs/**", "README*"],
             "created_at": "2026-10-27T07:58:05+00:00"},
}
FREE = ("kestrel", "heron", "plover")
BUSY = "osprey"


# ---------------------------------------------------------------- fixture helpers


def configure(case, lanes, roles):
    """Lanes, a runtime limit no test reaches, and either this repo's roles block or none."""
    from foragent import capacity

    path = case.root / ".foragent.yml"
    config = yaml.safe_load(path.read_text(encoding="utf-8"))
    config["lanes"] = {lane: {"paths": [f"{lane}/**"]} for lane in lanes}
    config["max_runtime_minutes"] = 100000
    if roles == "stored":
        config["roles"] = REPO_ROLES
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    # Machine run slots are not the limit in these fixtures; lanes are.
    capacity.write_state(case.store, 12, case.clock[0])
    case.busy = []


def approve(case, number, lane=None, labels=()):
    from foragent.issue import body_hash

    names = ["approved", "ready", *labels] + ([f"lane:{lane}"] if lane else [])
    case.tracker.add_issue(number, f"Approved work {number}", BODY, labels=names)
    case.store.save_approval(number, body=BODY, body_sha=body_hash(BODY), acceptance_blobs={},
                             approved_by="human", approved_at=case.clock[0].isoformat())


def unapproved(case, number, lane, body=BODY, state="open"):
    case.tracker.add_issue(number, f"Waiting for approval {number}", body, labels=[f"lane:{lane}"], state=state)


def busy(case, number, lane):
    """One live run in ``lane``; the tick only sees records (pid unknown, no process to probe)."""
    approve(case, number, lane)
    case.tracker.remove_labels(number, ["ready"])
    case.tracker.add_labels(number, ["running"])
    case.store.start_run(number, branch=f"foragent/issue-{number}", worktree=case.store.worktree_path(number),
                         lane=lane)
    case.busy.append(number)


def tick(case, capsys, minutes=5):
    """One gate tick ``minutes`` after the previous one; running runs keep reporting progress."""
    case.clock[0] += timedelta(minutes=minutes - 5)  # run_tick adds the other five minutes.
    for number in case.busy:
        case.store.update_run(number, last_progress_at=(case.clock[0] + timedelta(minutes=5)).isoformat())
    run_tick(case, capsys)


def gate_requests(case, capsys):
    return [row for row in inbox(case, capsys) if row.get("from") == "gate"]


def evidence(row):
    return json.dumps({key: row.get(key) for key in ("text", "facts", "signal")}, ensure_ascii=False, default=str)


def numbers(row):
    """Every count the signal states: integers in its facts and standalone numbers in its text."""
    found = set()

    def walk(value):
        if isinstance(value, bool):
            return
        if isinstance(value, int):
            found.add(value)
        elif isinstance(value, dict):
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(row.get("facts"))
    found.update(int(match) for match in re.findall(r"(?<![\w#./:-])(\d+)(?![\w/:-])", row.get("text") or ""))
    return found


def end_events(case):
    """Journal events that record the end (clear) of a signal."""
    path = case.store.root / "orch-events.jsonl"
    if not path.exists():
        return []
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [row for row in rows if "clear" in str(row.get("kind")) or str(row.get("kind")).endswith("-end")]


def answer(case, capsys, request_id):
    capsys.readouterr()
    code = cli_exit(case.main, ["orch", "signal-answer", request_id, "called", "--why",
                                "Ordered work for the idle lanes", "--repo-root", str(case.root)])
    output = capsys.readouterr()
    assert code == 0, f"A stall signal travels the role-signal path, so signal-answer answers it: {output.err}"


def cli(case, capsys, *argv):
    capsys.readouterr()
    code = cli_exit(case.main, [*argv, "--repo-root", str(case.root)])
    output = capsys.readouterr()
    assert code == 0, f"fa {' '.join(argv)} failed: {output.err}"
    return output.out


def populate(case, roles):
    """Four lanes; osprey runs #401; five approved startable issues cover every free lane and main;
    six open issues with a lane and Done when wait for approval; three look alike but do not count."""
    configure(case, (*FREE, BUSY), roles)
    busy(case, 401, BUSY)
    for number, lane in ((201, "kestrel"), (202, "kestrel"), (203, "heron"), (204, "plover"), (205, None)):
        approve(case, number, lane)
    for number, lane in zip(range(301, 307), ("kestrel", "heron", "plover", "kestrel", "heron", "plover")):
        unapproved(case, number, lane)
    unapproved(case, 311, "heron", body=NO_DONE_WHEN)  # No Done when: not ready for approval.
    unapproved(case, 312, "plover", state="closed")  # Closed: waits for nothing.
    approve(case, 313, "heron")  # Approved but not ready: neither startable nor unapproved.
    case.tracker.remove_labels(313, ["ready"])
    return case


@pytest.fixture
def lanes_case(tick_case):
    return populate(tick_case, "stored")


def starve(case):
    """Leave one startable approved issue (#201) for three free lanes."""
    for number in (202, 203, 204, 205):
        case.tracker.remove_labels(number, ["ready"])


def feed(case):
    for number in (202, 203, 204, 205):
        case.tracker.add_labels(number, ["ready"])


def assert_supply_signal(row, free=FREE, busy_lanes=(BUSY,), unapproved_count=6):
    text = evidence(row)
    assert row["status"] != "done", "The orchestrator receives an open request to act on"
    for lane in free:
        assert re.search(rf"\b{re.escape(lane)}\b", text), f"The supply-low signal must name free lane {lane}: {text}"
    for lane in busy_lanes:
        assert not re.search(rf"\b{re.escape(lane)}\b", text), f"Lane {lane} is busy, not free: {text}"
    assert unapproved_count in numbers(row), (
        f"The signal must say how many open issues with a lane and Done when wait for approval "
        f"({unapproved_count}; not closed ones, not those without Done when): {text}")


# ---------------------------------------------------------------- supply low


@pytest.mark.parametrize("roles", ["stored", "derived"], ids=["repo-roles-block", "no-roles-block"])
def test_supply_low_signals_once_after_30_minutes_and_records_its_end_once(tick_case, capsys, roles):
    case = populate(tick_case, roles)
    for _ in range(6):
        tick(case, capsys)
    assert gate_requests(case, capsys) == [], "Enough startable approved work for every free lane: no signal"

    starve(case)
    tick(case, capsys)
    low_since = case.clock[0]
    while case.clock[0] + timedelta(minutes=5) < low_since + timedelta(minutes=30):
        tick(case, capsys)
        assert gate_requests(case, capsys) == [], (
            f"Supply has been low for only {case.clock[0] - low_since}; the signal waits 30 minutes")
    tick(case, capsys)  # First tick at 30 minutes, not a later grace period.
    rows = gate_requests(case, capsys)
    assert len(rows) == 1, f"Supply low for 30 minutes sends exactly one signal, got {len(rows)}"
    first = rows[0]
    assert_supply_signal(first)
    assert end_events(case) == [], "The stall still lasts; no end is recorded"

    for _ in range(3):
        tick(case, capsys)
        assert [row["id"] for row in gate_requests(case, capsys)] == [first["id"]], (
            "An unanswered supply-low signal must not repeat while the stall lasts")
    answer(case, capsys, first["id"])
    for _ in range(8):
        tick(case, capsys, minutes=15)
    assert [row["id"] for row in gate_requests(case, capsys)] == [first["id"]], (
        "A supply-low stall that continues is not signalled again, answered or not")
    assert end_events(case) == []

    feed(case)
    for _ in range(4):
        tick(case, capsys)
    assert len(end_events(case)) == 1, "The end of the stall is recorded exactly once"
    assert [row["id"] for row in gate_requests(case, capsys)] == [first["id"]], "An end raises no new signal"

    starve(case)
    for _ in range(10):
        tick(case, capsys)
    rows = gate_requests(case, capsys)
    assert len(rows) == 2, "A new supply-low episode after the end is signalled once more"
    assert_supply_signal(next(row for row in rows if row["id"] != first["id"]))
    assert len(end_events(case)) == 1


def test_supply_low_counts_startable_work_not_approved_work(lanes_case, capsys):
    """Five approved issues for three free lanes, but four are not ready: the lanes still starve."""
    case = lanes_case
    tick(case, capsys)
    starve(case)
    for _ in range(9):
        tick(case, capsys)
    rows = gate_requests(case, capsys)
    assert len(rows) == 1, "Approved issues that cannot start do not feed a lane"
    assert_supply_signal(rows[0])


def test_short_dips_and_enough_supply_stay_quiet(lanes_case, capsys):
    case = lanes_case
    for _ in range(3):
        for _ in range(8):
            tick(case, capsys, minutes=15)
        starve(case)
        for _ in range(4):  # Low for 20 minutes only.
            tick(case, capsys)
        feed(case)
    for _ in range(4):
        tick(case, capsys)
    assert gate_requests(case, capsys) == [], (
        "Supply that covers the free lanes, or dips under them for less than 30 minutes, is no stall")
    assert end_events(case) == []


def test_replay_of_11_05_09_44_fires_supply_low(tick_case, capsys):
    """The 11-05 09:44 KST (00:44Z) records, reduced to what the signal reads.

    From the gate's runs.jsonl at 00:44Z: #1492 ran in view, #1854 in orch and #1952 in main;
    no approved issue could start; 28 open issues with a lane and Done when waited only for
    approval (#2088). Lanes are this repository's ten lanes; roles are its roles block.
    """
    case = tick_case
    lanes = ("adopt", "order", "bench", "orch", "view", "board", "skills", "design", "review-engine", "roles")
    case.clock[0] = datetime(2026, 11, 5, 0, 0, tzinfo=UTC)
    configure(case, lanes, "stored")
    for number, lane in ((1492, "view"), (1854, "orch"), (1952, None)):
        busy(case, number, lane)
    idle = [lane for lane in lanes if lane not in ("view", "orch")]
    for index in range(28):
        unapproved(case, 2100 + index, lanes[index % len(lanes)])
    while case.clock[0] < datetime(2026, 11, 5, 0, 45, tzinfo=UTC):
        tick(case, capsys)
    rows = gate_requests(case, capsys)
    assert len(rows) == 1, f"On the 11-05 09:44 records the supply-low signal fires once, got {len(rows)}"
    # "orch" and "view" are also words of ordinary signal text (`fa orch ...`), so only the free
    # lanes and the count are checked here; the busy-lane check is in the tests above.
    assert_supply_signal(rows[0], free=idle, busy_lanes=(), unapproved_count=28)


# ---------------------------------------------------------------- requests stuck in processing


def make_request(case, capsys, text):
    output = cli(case, capsys, "orch", "request", "--from", "orchestrator", "--text", text)
    match = re.search(r"Request (\S+) saved", output)
    assert match, output
    cli(case, capsys, "orch", "request-status", match[1], "processing")
    return match[1]


def test_request_processing_for_hours_is_signalled_once_and_shown_to_the_pm(lanes_case, capsys):
    case = lanes_case
    created = case.clock[0]
    stuck = make_request(case, capsys, "Turn cleanup order A into issues (r564)")
    while case.clock[0] + timedelta(minutes=30) < created + timedelta(hours=3):
        tick(case, capsys, minutes=30)
        assert gate_requests(case, capsys) == [], "A processing request is not signalled before three hours"
    tick(case, capsys, minutes=29)
    assert case.clock[0] == created + timedelta(hours=3, minutes=-1)
    assert gate_requests(case, capsys) == [], "No signal even one minute before three hours"
    fresh = make_request(case, capsys, "A request taken a moment ago")
    tick(case, capsys, minutes=2)
    rows = gate_requests(case, capsys)
    assert len(rows) == 1, f"Just after three hours sends exactly one processing-stall signal, got {len(rows)}"
    signal = rows[0]
    assert signal["status"] != "done"
    assert stuck in evidence(signal), "The signal names the stalled request"
    assert fresh not in evidence(signal), "A request processing for minutes is not part of the stall"

    shown = cli(case, capsys, "brief", "--json")
    assert stuck in shown, "The PM's briefing shows the stalled request"

    cli(case, capsys, "orch", "request-status", fresh, "done", "--answer", "Done at once")
    for _ in range(8):
        tick(case, capsys, minutes=30)
    assert [row["id"] for row in gate_requests(case, capsys)] == [signal["id"]], (
        "A request that stays in processing is not signalled again")
    assert end_events(case) == []

    cli(case, capsys, "orch", "request-status", stuck, "done", "--answer", "Published as issues")
    for _ in range(3):
        tick(case, capsys)
    assert len(end_events(case)) == 1, "The end of the request stall is recorded once"
    assert [row["id"] for row in gate_requests(case, capsys)] == [signal["id"]]


# ---------------------------------------------------------------- parked issues whose reason resolved


def park(case, capsys, number, why):
    cli(case, capsys, "orch", "park", str(number), "--why", why)


@pytest.mark.parametrize("how", ["closed", "merged"])
def test_parked_issue_whose_reason_merged_or_closed_is_signalled_once_for_review(lanes_case, capsys, how):
    case = lanes_case
    unapproved(case, 600, "heron")  # The replacement the park reason waits for.
    unapproved(case, 601, "plover")  # Another issue that stays open.
    for number, lane in ((501, "heron"), (502, "plover"), (503, None)):
        approve(case, number, lane)  # At most two ready issues per lane: no idle-lanes-with-backlog signal.
    tick(case, capsys)
    park(case, capsys, 501, "Replaced by the merge owner #600; revisit when #600 merges")
    park(case, capsys, 502, "Waits for #601 to land first")
    park(case, capsys, 503, "Waits for the person's pricing decision")
    for _ in range(24):
        tick(case, capsys, minutes=60)
    assert gate_requests(case, capsys) == [], "A park reason that still waits on open work is no stall"

    if how == "merged":
        case.store.start_run(600, branch="foragent/issue-600", worktree=case.store.worktree_path(600), lane="heron")
        case.store.record_merge(600, case.clock[0])
        case.store.finish_run(600, outcome="merged")
        case.tracker.close_issue(600, reason="completed")
    else:
        case.tracker.close_issue(600, reason="not_planned")
    for _ in range(3):
        tick(case, capsys)
    rows = gate_requests(case, capsys)
    assert len(rows) == 1, f"A parked issue whose reason {how} is signalled once for review, got {len(rows)}"
    signal = rows[0]
    text = evidence(signal)
    assert signal["status"] != "done"
    assert re.search(r"(?<!\d)501(?!\d)", text), f"The signal names the parked issue #501: {text}"
    assert not re.search(r"(?<!\d)50[23](?!\d)", text), f"#502 and #503 still wait: {text}"

    for _ in range(24):
        tick(case, capsys, minutes=60)
    assert [row["id"] for row in gate_requests(case, capsys)] == [signal["id"]], (
        "A parked issue that stays parked is not signalled again")
    assert end_events(case) == []

    cli(case, capsys, "orch", "unpark", "501")
    for _ in range(3):
        tick(case, capsys)
    assert len(end_events(case)) == 1, "Unparking ends the stall; its end is recorded once"
    assert [row["id"] for row in gate_requests(case, capsys)] == [signal["id"]]
