"""Shared world for stale-base-main-red acceptance tests (2026-11-09, PM r1433).

Builds on the locked closed-lock worlds (closed_lock_red_support, closed_lock_every_capture_support):
offline FakeTracker, real `fa gate tick --no-start` (the gate pass, which reads main's recorded
acceptance results from the machine-wide baseline cache), `fa orch inbox --json` and
`fa brief --report --json` (the /fa report), whose ``broken_promises`` list is the main-red promise store as the brief shows
it ("broken on main"). No test run, network or process signal is involved.

The recorded incident: one capture of main at a641b3dd (11-05) failed locked tests of closed
issues. Every later capture of main ran only part of the suite and never ran those files, so the
stale failure stayed the newest reading of those tests and 59 entries stayed open while main
passes them. ``stale_world`` rebuilds that shape: an old main commit ``stale`` (four days and
several commits behind) and a later main commit ``latest``. Per the PM answer, only a later main
measurement that ran the test and saw it pass outranks the stale failure
(``capture_latest_green``); a later capture that did not run it (``capture_latest_elsewhere``)
changes nothing (#2951).
"""

from datetime import timedelta
import json

from closed_lock_every_capture_support import capture, new_commit
from closed_lock_red_support import ALL_NODES, CLOSED_FILE, CLOSED_ISSUE, CLOSED_NODE, names, names_issue
from test_skills_autosync import cli_exit
from test_tick_failure_alert import inbox

# A partial capture that never ran the closed lock's file (as main's later captures never ran
# test_box_facts_new_requests.py and its neighbours).
ELSEWHERE = tuple(node for node in ALL_NODES if not node.startswith(CLOSED_FILE + "::"))


def stale_world(case):
    """``case.stale``: an old main commit; ``case.latest``: main's newest commit, four days and
    several commits later (``case.stale`` is its older ancestor). Nothing is recorded for either yet."""
    case.stale = new_commit(case)
    case.stale_time = case.clock[0] - timedelta(minutes=1)
    for _ in range(3):
        new_commit(case)
    case.clock[0] += timedelta(days=4)
    case.latest = new_commit(case)
    return case


def capture_stale_failure(case, node=CLOSED_NODE):
    """The stale capture: the full suite ran at ``case.stale`` four days ago and ``node`` failed."""
    return capture(case, case.stale, failing=[node], created_at=case.stale_time)


def capture_latest_elsewhere(case, sha=None):
    """A partial capture of main (``case.latest`` by default) that ran every file but the closed
    lock's: it did not run the test."""
    return capture(case, sha or case.latest, collected=ELSEWHERE)


def capture_latest_green(case, sha=None, created_at=None):
    """A capture of main (``case.latest`` by default) that ran every test and saw them pass."""
    return capture(case, sha or case.latest, created_at=created_at)


def observe_ci(case, sha, collected, passing):
    """One CI acceptance comparison accepted reports whose base side measured main at ``sha``
    (the call ci_mode.read makes with the reports it accepted)."""
    from foragent import orch_facts_promises
    from foragent.config import load_config
    from foragent.scheduler import TickContext
    from foragent.state import StateStore

    case.store = StateStore(case.store.root, now=lambda: case.clock[0])
    ctx = TickContext(repo="owner/repo", repo_root=case.root, tracker=case.tracker, store=case.store,
                      config=load_config(case.root), git=case.git, now=lambda: case.clock[0],
                      launcher=lambda n: 7000 + n, runner_process_exists=lambda n: True,
                      kill_runner=lambda pid: None)
    reports = [{"report": {"base_sha": sha, "base": {
        "complete": True, "collected_ids": sorted(collected), "passing_ids": sorted(passing)}}}]
    orch_facts_promises.observe(ctx, reports)


def broken_on_main(case, capsys):
    """The tests `fa brief --report --json` lists as broken on main (open main-red entries)."""
    capsys.readouterr()
    code = cli_exit(case.main, ["brief", "--report", "--json", "--repo-root", str(case.root)])
    output = capsys.readouterr()
    assert code == 0, f"fa brief --report --json failed: {output.err}"
    shown = json.loads(output.out)
    assert "broken_promises" in shown, f"fa brief --report --json lists no main-red entries: {sorted(shown)}"
    return {row["test"]: row for row in shown["broken_promises"]}


def cleared_events(case, node=CLOSED_NODE):
    """Journal records that the entry for ``node`` closed (``promise-cleared``)."""
    path = case.store.root / "orch-events.jsonl"
    if not path.exists():
        return []
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [row for row in rows if row.get("kind") == "promise-cleared"
            and node in json.dumps(row, ensure_ascii=False, default=str)]


def assert_no_entry(case, capsys, node=CLOSED_NODE, issue=CLOSED_ISSUE, why=""):
    broken = broken_on_main(case, capsys)
    assert node not in broken, f"{why}; the brief must not list {node} as broken on main: {broken}"
    for row in inbox(case, capsys):
        assert not names(row, node), f"{why}; no request may name {node}: {row}"
        assert not names_issue(row, issue), f"{why}; closed issue #{issue} broke nothing on main: {row}"
