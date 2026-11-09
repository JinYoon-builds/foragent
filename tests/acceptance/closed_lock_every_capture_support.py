"""Shared world for closed-lock-alarm-every-capture acceptance tests (2026-11-09, r1324).

Builds on the locked closed-lock-red-alarm world (closed_lock_red_support): offline FakeTracker,
real `fa gate tick --no-start`, `fa orch inbox --json`, and main's recorded acceptance results in
the machine-wide baseline cache ``<state parent>/baselines/<key>.json``.

Additions here:

- ``capture`` writes one recorded capture of a main commit with any collected / passing /
  failing split, so a capture can be partial (it ran only some tests) or show a test skipped or
  xfailed (collected, neither passing nor failing, as the runner's JUnit reading records it).
  Its ``created_at`` is given, so a capture of an older commit can be recorded as older.
- ``pre_upgrade_alarm`` leaves the records the first alarm version (#2334, D122) kept before
  recorded captures were read: an open orchestrator request from the gate naming the test, its
  closed issue and a main commit, and ``promises.json`` in that version's shape
  ``{"latest_base": sha, "open": {test: {issue, request, since, base_sha}}}``; no capture of
  that commit exists in the cache.
"""

from datetime import timedelta
from hashlib import sha256
import json

from closed_lock_red_support import ALL_NODES, BODY, CLOSED_ISSUE, CLOSED_NODE, commit


def capture(case, sha, *, collected=ALL_NODES, failing=(), unknown=(), created_at=None, name=None):
    """Record a capture of ``sha``: ``collected`` ran, ``failing`` failed, ``unknown`` were
    skipped or xfailed, every other collected test passed."""
    case.records += 1
    collected = sorted(collected)
    failing = sorted(failing)
    unknown = set(unknown)
    created_at = created_at or case.clock[0] - timedelta(minutes=1) + timedelta(seconds=case.records)
    payload = {
        "node_ids": [node for node in collected if node not in failing and node not in unknown],
        "collected_ids": collected, "failing_ids": failing, "excluded": [],
        "repo": "owner/repo", "repository": str(case.root.resolve()), "base_sha": sha,
        "created_at": created_at.isoformat(), "exit_code": 1 if failing else 0, "timed_out": False,
        "duration_seconds": 600.0,
    }
    key = name or sha256(f"closed-lock-every-capture:{case.records}:{sha}".encode()).hexdigest()
    path = case.cache / f"{key}.json"
    path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    return path


def new_commit(case, *, minutes=20):
    case.clock[0] += timedelta(minutes=minutes)
    return commit(case)


def pre_upgrade_alarm(case, sha, node=CLOSED_NODE, issue=CLOSED_ISSUE):
    """The first alarm version's records of an open report on ``sha`` (no capture of it kept)."""
    from foragent import orch_inbox

    text = (f"Closed issue #{issue}'s locked test {node} fails on main ({sha[:12]}). "
            "Its behaviour is broken; order a fix.")
    record = orch_inbox.add_request(case.store, "gate", text,
                                    {"promise": {"test": node, "issue": issue, "base_sha": sha}})
    promises = {"latest_base": sha, "open": {node: {
        "issue": issue, "request": record["id"], "since": case.store._timestamp(), "base_sha": sha}}}
    (case.store.root / "promises.json").write_text(json.dumps(promises) + "\n", encoding="utf-8")
    return record


def reopen(case, issue=CLOSED_ISSUE):
    """The locked issue is open (its tests are red by design until it ships)."""
    case.tracker.add_issue(issue, f"Locked work {issue}", BODY, labels=["approved", "ready"], state="open")


def close(case, issue=CLOSED_ISSUE):
    case.tracker.close_issue(issue)
