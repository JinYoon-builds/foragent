"""Shared world for closed-lock-red-alarm acceptance tests (2026-11-09).

Only public entry points are driven: `fa gate tick --no-start` (the gate pass), `fa orch inbox
--json` (what the orchestrator receives) and `fa orch request-status` (the orchestrator closing a
request). Issues live in the offline FakeTracker; no process is started or signalled.

"Main's recorded acceptance results" are the gate's base results: the machine-wide baseline cache
``<state parent>/baselines/<key>.json`` that the runner's comparisons read (run_baseline). Each
record is one measured main commit: ``base_sha``, ``created_at``, ``collected_ids``,
``failing_ids`` and ``node_ids`` (passing), tagged with this repository. Every recorded commit is a
real commit on ``origin/main`` of the fixture repository, fetched into the checkout.

A locked test belongs to the issue whose approval lists its file in ``acceptance_blobs``; the
issue is open or closed in the FakeTracker.

What counts as "a request naming the test": an inbox request whose record (text, facts or any
other field) holds the test's node id, or both its file and its function name. Field names are
the lane's choice; the checks search the whole record.
"""

from datetime import timedelta
from hashlib import sha256
import json
import re

from conftest import git_command
from test_tick_failure_alert import inbox, run_tick

BODY = ("<!-- foragent:v1 -->\n## Goal\nDo the work.\n## Out of scope\nOther work.\n"
        "## Done when\n```sh\ntrue\n```\n")

CLOSED_ISSUE = 2737
CLOSED_FILE = "tests/acceptance/test_lane_m2_bubble.py"
CLOSED_TEST = "test_acorn_click_opens_the_bubble"
CLOSED_NODE = f"{CLOSED_FILE}::{CLOSED_TEST}"
CLOSED_PASSING = f"{CLOSED_FILE}::test_bubble_closes_on_escape"

OTHER_CLOSED_ISSUE = 2650
OTHER_CLOSED_FILE = "tests/acceptance/test_quiet_hours_badge.py"
OTHER_CLOSED_TEST = "test_badge_hides_after_quiet_hours"
OTHER_CLOSED_NODE = f"{OTHER_CLOSED_FILE}::{OTHER_CLOSED_TEST}"

OPEN_ISSUE = 2901
OPEN_FILE = "tests/acceptance/test_pending_feature_lock.py"
OPEN_TEST = "test_pending_feature_not_built_yet"
OPEN_NODE = f"{OPEN_FILE}::{OPEN_TEST}"

ALL_NODES = (CLOSED_NODE, CLOSED_PASSING, OTHER_CLOSED_NODE, OPEN_NODE)


def setup(case):
    """Lock files on main, their issues' approvals, and an empty baseline cache."""
    case.cache = case.store.root.parent / "baselines"
    case.cache.mkdir(parents=True, exist_ok=True)
    case.records = 0
    files = {
        CLOSED_FILE: f"def {CLOSED_TEST}():\n    pass\n\n\ndef test_bubble_closes_on_escape():\n    pass\n",
        OTHER_CLOSED_FILE: f"def {OTHER_CLOSED_TEST}():\n    pass\n",
        OPEN_FILE: f"def {OPEN_TEST}():\n    pass\n",
    }
    first = commit(case, files, "Add locked tests")
    blobs = {path: git_command(case.history.origin, "rev-parse", f"{first}:{path}") for path in files}
    lock(case, CLOSED_ISSUE, {CLOSED_FILE: blobs[CLOSED_FILE]}, state="closed")
    lock(case, OTHER_CLOSED_ISSUE, {OTHER_CLOSED_FILE: blobs[OTHER_CLOSED_FILE]}, state="closed")
    lock(case, OPEN_ISSUE, {OPEN_FILE: blobs[OPEN_FILE]}, state="open")
    return first


def lock(case, number, blobs, *, state):
    from foragent.issue import body_hash

    labels = ["approved"] if state == "closed" else ["approved", "ready"]
    case.tracker.add_issue(number, f"Locked work {number}", BODY, labels=labels, state=state)
    case.store.save_approval(number, body=BODY, body_sha=body_hash(BODY), acceptance_blobs=blobs,
                             approved_by="human", approved_at=case.clock[0].isoformat())


def commit(case, files=None, message="Main change"):
    """One new commit on origin/main, fetched into the checkout; returns its full SHA."""
    case.commits = getattr(case, "commits", 0) + 1
    files = files or {f"notes/change-{case.commits}.txt": f"change {case.commits}\n"}
    case.history.update(files, at=case.clock[0] - timedelta(minutes=30), message=message)
    git_command(case.root, "fetch", "--quiet", "origin")
    sha = case.history.tip
    assert git_command(case.root, "rev-parse", "origin/main") == sha
    return sha


def measure(case, sha, failing):
    """Record main's acceptance results at ``sha``: every known node ran; ``failing`` did not pass.

    The record is stamped a minute before now, so the next gate pass reads it.
    """
    case.records += 1
    failing = sorted(failing)
    collected = sorted(ALL_NODES)
    created_at = case.clock[0] - timedelta(minutes=1) + timedelta(seconds=case.records)
    payload = {
        "node_ids": [node for node in collected if node not in failing],
        "collected_ids": collected, "failing_ids": failing, "excluded": [],
        "repo": "owner/repo", "repository": str(case.root.resolve()), "base_sha": sha,
        "created_at": created_at.isoformat(), "exit_code": 1 if failing else 0, "timed_out": False,
        "duration_seconds": 600.0,
    }
    key = sha256(f"closed-lock-red:{case.records}:{sha}".encode()).hexdigest()
    (case.cache / f"{key}.json").write_text(json.dumps(payload) + "\n", encoding="utf-8")


def red_main(case, failing, *, minutes=20):
    """Advance the clock, add one main commit, and record ``failing`` as its results."""
    case.clock[0] += timedelta(minutes=minutes)
    sha = commit(case)
    measure(case, sha, failing)
    return sha


def gate_pass(case, capsys):
    run_tick(case, capsys)


def text_of(row):
    return json.dumps(row, ensure_ascii=False, default=str)


def names(row, node):
    file, test = node.split("::", 1)
    text = text_of(row)
    return node in text or (file in text and test in text)


def requests_naming(case, capsys, node):
    return [row for row in inbox(case, capsys) if names(row, node)]


def names_issue(row, number):
    return re.search(rf"(?<![\w.-]){number}(?!\d)", text_of(row)) is not None


def names_commit(row, sha):
    """The commit's SHA, full or abbreviated to at least seven characters."""
    return sha[:7] in text_of(row)
