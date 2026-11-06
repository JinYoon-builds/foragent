"""Closed issues' locked tests stay green on main (D122).

Every comparison that reads CI acceptance reports (run, merge owner, merge
batch) passes the reports it accepted to ``observe``. The base side of those
reports is main as CI measured it, so no test run is started here. A locked
acceptance test of a closed issue that fails there is reported once to the
orchestrator request box; it stays open in ``promises.json`` until a later
reading shows it passing, which appends one ``promise-cleared`` event.
``promises.json`` also keeps the newest main read (``latest_base``); a reading
of an older main changes nothing.

``observe`` is the only writer of ``promises.json``; the /fa report reads it.
"""

from __future__ import annotations

import json
from typing import Any

from foragent.orch_events import append_event
from foragent.state_locks import _file_lock

FILE = "promises.json"


def _saved(store) -> dict[str, Any]:
    try:
        value = json.loads((store.root / FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def open_promises(store) -> dict[str, dict[str, Any]]:
    """Broken promises still failing on main: test id -> {issue, request, since, base_sha}."""
    value = _saved(store).get("open")
    if not isinstance(value, dict):
        return {}
    return {test: row for test, row in value.items()
            if isinstance(test, str) and isinstance(row, dict) and type(row.get("issue")) is int}


def _older(git, base: str, latest: str | None) -> bool:
    """True only when ``base`` is a strict ancestor of the newest main already read.

    Readings arrive in any order (a run, the merge owner and a batch read
    different PRs), so an older main never overrides a newer one. An unknown
    relation (a commit this clone lacks, diverged history) is not older.
    """
    if not latest or latest == base:
        return False
    try:
        return bool(git.is_ancestor(base, latest))
    except Exception:  # noqa: BLE001 - unknown order reads as not older
        return False


def _base_side(reports) -> tuple[str | None, set[str], set[str]]:
    from foragent.ci_mode import reported_base, suite

    collected, passing = set(), set()
    for item in reports:
        ids, passed = suite(item["report"]["base"], "report.base")[:2]
        collected |= ids
        passing |= passed
    return reported_base(reports), collected, passing


def _approvals(store) -> dict[int, set[str]]:
    owners = {}
    for path in sorted((store.root / "approvals").glob("*.json")):
        try:
            number = int(path.stem)
            approval = store.load_approval(number)
        except (ValueError, OSError):
            continue
        if approval is not None:
            owners[number] = set(approval.acceptance_blobs or {})
    return owners


def _state(ctx, number: int, rows: dict[int, dict]) -> str | None:
    row = rows.get(number)
    if row is not None and row.get("state"):
        return row["state"]
    try:
        return ctx.tracker.get_issue(number).state
    except Exception:  # noqa: BLE001 - unknown ownership reports nothing
        return None


def _owner(ctx, path: str, approvals, rows) -> int | None:
    """The closed issue owning a locked file; None when any owner is open or unknown."""
    owners = sorted(number for number, files in approvals.items() if path in files)
    states = [_state(ctx, number, rows) for number in owners]
    if not owners or any(state != "closed" for state in states):
        return None
    return owners[-1]


def observe(ctx, reports) -> None:
    """Read main's results from accepted reports; never changes the comparison's result."""
    try:
        _observe(ctx, reports)
    except Exception:  # noqa: BLE001 - an observation never replaces the comparison
        return


def _observe(ctx, reports) -> None:
    from foragent import orch_inbox

    store = ctx.store
    base, collected, passing = _base_side(reports)
    if base is None:
        return  # Shards that disagree on main cannot be placed in main's order.
    failing = collected - passing
    acceptance = ctx.config.acceptance_dir.rstrip("/") + "/"
    with _file_lock(store.root / "promises.lock"):
        latest = _saved(store).get("latest_base")
        if _older(getattr(ctx, "git", None), base, latest if isinstance(latest, str) else None):
            return
        promises = open_promises(store)
        changed = latest != base
        for test in sorted(set(promises) & passing):
            row = promises.pop(test)
            changed = True
            append_event(store, store._now(), row["issue"], "promise-cleared",
                         f"#{row['issue']} {test} passes on main again", test=test, base_sha=base,
                         request=row.get("request"))
        candidates = sorted(test for test in failing - set(promises) if test.startswith(acceptance))
        if candidates:
            approvals = _approvals(store)
            rows = {row["number"]: row for row in store.local_issues() if type(row.get("number")) is int}
            for test in candidates:
                number = _owner(ctx, test.split("::", 1)[0], approvals, rows)
                if number is None:
                    continue
                record = orch_inbox.add_request(
                    store, "gate", f"Closed issue #{number}'s locked test {test} fails on main"
                    f" ({base[:12]}). Its behaviour is broken; order a fix.",
                    {"promise": {"test": test, "issue": number, "base_sha": base}})
                promises[test] = {"issue": number, "request": record["id"],
                                  "since": store._timestamp(), "base_sha": base}
                changed = True
        if changed:
            store._write_json(store.root / FILE, {"latest_base": base, "open": promises})
