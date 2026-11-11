"""Closed issues' locked tests stay green on main (D122, #2925).

Main's acceptance results reach this module two ways: every comparison that
reads CI acceptance reports (run, merge owner, merge batch) passes the reports
it accepted to ``observe``, and each gate tick calls ``observe_baselines``,
which reads the gate's own baseline captures (the base the runner's
comparisons read). No test run is started here.

Each reading is kept as a small digest in ``promise-readings.json``: the
acceptance tests that failed, the ones whose result says nothing (skipped,
xfailed, or a CI side that cannot tell), and the acceptance files collected
(a collected test in neither list passed). A baseline capture is digested once
per file version, whatever order captures finish in. There is no read cursor:
every evaluation orders all kept readings by main's commit order, so a late
reading of an older commit still counts in its place.

A locked acceptance test of a closed issue whose newest reading on main fails
is reported once to the orchestrator request box, naming the first commit of
its current red run (the first failing reading after its last passing one).
It stays open in ``promises.json`` until its newest reading passes, which
appends one ``promise-cleared`` event; a later red run is a new report. A
reading that shows the red run began earlier moves the open report's commit
back. This module is the only writer of both files, under ``promises.lock``;
the /fa report reads ``promises.json``.
"""

from __future__ import annotations

from functools import cmp_to_key
from hashlib import sha256
import json
from typing import Any

from foragent.orch_events import append_event
from foragent.state_locks import _file_lock

FILE = "promises.json"
READINGS = "promise-readings.json"
# CI readings kept, newest first; baseline digests live as long as their capture file.
KEEP_CI = 200


def _load(store, name: str) -> dict[str, Any]:
    try:
        value = json.loads((store.root / name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _saved(store) -> dict[str, Any]:
    return _load(store, FILE)


def open_promises(store) -> dict[str, dict[str, Any]]:
    """Broken promises still failing on main: test id -> {issue, request, since, base_sha}."""
    value = _saved(store).get("open")
    if not isinstance(value, dict):
        return {}
    return {test: row for test, row in value.items()
            if isinstance(test, str) and isinstance(row, dict) and type(row.get("issue")) is int}


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


def _state(ctx, number: int, rows: dict[int, dict], ask_tracker: bool = True) -> str | None:
    row = rows.get(number)
    if row is not None and row.get("state"):
        return row["state"]
    if not ask_tracker:
        return None  # The gate tick reads no GitHub here: an unknown owner reports nothing.
    try:
        return ctx.tracker.get_issue(number).state
    except Exception:  # noqa: BLE001 - unknown ownership reports nothing
        return None


def _owner(ctx, path: str, approvals, rows, ask_tracker: bool = True) -> int | None:
    """The closed issue owning a locked file; None when any owner is open or unknown."""
    owners = sorted(number for number, files in approvals.items() if path in files)
    states = [_state(ctx, number, rows, ask_tracker) for number in owners]
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
    base, collected, passing = _base_side(reports)
    if base is None:
        return  # Shards that disagree on main cannot be placed in main's order.
    # A CI report lists collected and passing ids only, so every other collected test reads as failing.
    digest = _digest(ctx, base, collected, passing, collected - passing)
    with _file_lock(ctx.store.root / "promises.lock"):
        readings = _load(ctx.store, READINGS)
        ci = [row for row in readings.get("ci", []) if isinstance(row, dict)]
        digest["seq"] = _next_seq(readings)
        readings["ci"] = ([digest] + ci)[:KEEP_CI]
        _keep_files(readings)
        ctx.store._write_json(ctx.store.root / READINGS, readings)
        _evaluate(ctx, readings, ask_tracker=True)


def observe_baselines(ctx) -> None:
    """Digest new baseline captures of main, then evaluate; a failure never stops the tick."""
    try:
        _observe_baselines(ctx)
    except Exception:  # noqa: BLE001 - an observation never stops the tick
        return


def _observe_baselines(ctx) -> None:
    from foragent.run_baseline_limits import belongs

    store = ctx.store
    cache = store.root.parent / "baselines"
    with _file_lock(store.root / "promises.lock"):
        readings = _load(store, READINGS)
        known = readings.get("baselines") if isinstance(readings.get("baselines"), dict) else {}
        kept, changed = {}, False
        for path in sorted(cache.glob("*.json")):
            try:
                stat = path.stat()
            except OSError:
                continue
            version = f"{stat.st_mtime_ns}:{stat.st_size}"
            old = known.get(path.stem)
            if isinstance(old, dict) and old.get("version") == version:
                kept[path.stem] = old
                continue
            changed = True
            # Every capture file not yet digested is read, whatever its time stamp says.
            entry = {"version": version}
            try:
                saved = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(saved, dict) and belongs(saved, path, ctx.repo_root, ctx.config.repo):
                    entry |= _baseline_digest(ctx, saved) or {}
            except (OSError, ValueError, TypeError):
                pass
            if "sha" in entry:
                entry["seq"] = _next_seq(readings)
            kept[path.stem] = entry
        changed |= set(known) != set(kept)
        readings["baselines"] = kept
        if changed:
            _keep_files(readings)
            store._write_json(store.root / READINGS, readings)
        _evaluate(ctx, readings, ask_tracker=False)


def _baseline_digest(ctx, saved: dict[str, Any]) -> dict[str, Any] | None:
    """A finished capture of a commit; its failing ids are the failures and errors JUnit recorded."""
    base, passing = saved.get("base_sha"), saved.get("node_ids")
    if saved.get("timed_out") is True or not isinstance(base, str) or not isinstance(passing, list):
        return None
    failing = saved.get("failing_ids")
    collected = saved.get("collected_ids")
    collected = set(collected) if isinstance(collected, list) else set(passing) | set(
        failing if isinstance(failing, list) else [])
    # Without failing ids a non-passing test is no evidence of failure (skipped, xfailed).
    failing = set(failing) if isinstance(failing, list) else set()
    return _digest(ctx, base, collected, set(passing), failing)


def _digest(ctx, base: str, collected: set[str], passing: set[str], failing: set[str]) -> dict[str, Any]:
    acceptance = ctx.config.acceptance_dir.rstrip("/") + "/"
    collected = {test for test in collected if isinstance(test, str) and test.startswith(acceptance)}
    failing = (failing & collected) - passing
    files = sorted({test.split("::", 1)[0] for test in collected})
    return {"sha": base, "at": ctx.store._timestamp(), "failing": sorted(failing),
            "unknown": sorted(collected - passing - failing), "files": files}


def _next_seq(readings: dict[str, Any]) -> int:
    seq = readings.get("seq") if type(readings.get("seq")) is int else 0
    readings["seq"] = seq + 1
    return seq + 1


def _keep_files(readings: dict[str, Any]) -> None:
    """Store each distinct collected-file list once; readings refer to it by digest."""
    sets = readings.get("file_sets") if isinstance(readings.get("file_sets"), dict) else {}
    used = {}
    for row in _rows(readings):
        files = row.pop("files", None)
        if isinstance(files, list):
            key = sha256("\n".join(files).encode()).hexdigest()[:16]
            sets.setdefault(key, files)
            row["file_set"] = key
        if row.get("file_set") in sets:
            used[row["file_set"]] = sets[row["file_set"]]
    readings["file_sets"] = used


def _rows(readings: dict[str, Any]) -> list[dict[str, Any]]:
    baselines = readings.get("baselines") if isinstance(readings.get("baselines"), dict) else {}
    rows = [row for row in readings.get("ci", []) if isinstance(row, dict)]
    return rows + [row for row in baselines.values() if isinstance(row, dict)]


def _status(row: dict[str, Any], files: dict[str, Any], test: str) -> str | None:
    if test in row.get("failing", ()):
        return "fail"
    if test in row.get("unknown", ()):
        return None
    return "pass" if test.split("::", 1)[0] in files.get(row.get("file_set"), ()) else None


def _order(ctx, shas: set[str], first_seq: dict[str, int]) -> dict[str, int]:
    """Rank each commit on main, older lower; a commit main does not hold gets no rank.

    The checkout's main history decides. A git without history listing compares commits by
    ancestry, and with no git at all readings keep the order they arrived in.
    """
    git = getattr(ctx, "git", None)
    main = f"origin/{getattr(ctx.config, 'base_branch', 'main')}"
    if git is not None and hasattr(git, "_call"):
        try:
            listed = git._call(["rev-list", "--topo-order", "--max-count=20000", main]).split()
        except Exception:  # noqa: BLE001 - no main history: nothing can be placed
            return {}
        position = {sha: len(listed) - index for index, sha in enumerate(listed)}
        return {sha: position[sha] for sha in shas if sha in position}

    def compare(left: str, right: str) -> int:
        try:
            if git is not None and git.is_ancestor(left, right):
                return -1
            if git is not None and git.is_ancestor(right, left):
                return 1
        except Exception:  # noqa: BLE001 - unknown relation keeps arrival order
            pass
        return first_seq[left] - first_seq[right]

    ordered = sorted(shas, key=cmp_to_key(compare))
    return {sha: index for index, sha in enumerate(ordered)}


def _with_merge(ctx, number: int) -> set[str] | None:
    """Main commits that contain issue ``number``'s merge; None when main shows no such merge.

    The merge owner lands an issue as a merge commit with the trailer ``Foragent-Member: #N``. A
    commit measured before that merge (the batch base) cannot show whether the issue's behaviour
    holds, so only the merge commit and its descendants on main count.
    """
    git = getattr(ctx, "git", None)
    if git is None or not hasattr(git, "_call"):
        return None
    main = f"origin/{getattr(ctx.config, 'base_branch', 'main')}"
    try:
        merges = git._call(["log", "--format=%H", f"--grep=^Foragent-Member: #{number} ", main]).split()
        if not merges:
            return None
        merge = merges[-1]  # The first landing; a later one still descends from main's history.
        after = git._call(["rev-list", "--ancestry-path", f"{merge}..{main}"]).split()
    except Exception:  # noqa: BLE001 - unknown merge keeps every reading, as before
        return None
    return {merge, *after}


def _evaluate(ctx, readings: dict[str, Any], *, ask_tracker: bool) -> None:
    """Bring ``promises.json`` in line with every kept reading, in main's commit order."""
    from foragent import orch_inbox

    store = ctx.store
    saved = _saved(store)
    promises = open_promises(store)
    rows = sorted((row for row in _rows(readings) if isinstance(row.get("sha"), str) and type(row.get("seq")) is int),
                  key=lambda row: row["seq"])
    files = readings.get("file_sets") if isinstance(readings.get("file_sets"), dict) else {}
    approvals = None
    issues = None
    tests = set(promises)
    for test in sorted({test for row in rows for test in row.get("failing", ())} - tests):
        if approvals is None:
            approvals = _approvals(store)
            issues = {row["number"]: row for row in store.local_issues() if type(row.get("number")) is int}
        if _owner(ctx, test.split("::", 1)[0], approvals, issues, ask_tracker=ask_tracker) is not None:
            tests.add(test)
    if not tests:
        return
    first_seq = {}
    for row in rows:
        first_seq.setdefault(row["sha"], row["seq"])
    rank = _order(ctx, set(first_seq), first_seq)
    changed = False
    merged = {}
    for test in sorted(tests):
        open_row = promises.get(test)
        if open_row is not None:
            number = open_row.get("issue")
        else:
            if approvals is None:
                approvals = _approvals(store)
                issues = {row["number"]: row for row in store.local_issues() if type(row.get("number")) is int}
            number = _owner(ctx, test.split("::", 1)[0], approvals, issues, ask_tracker=ask_tracker)
        if type(number) is int and number not in merged:
            merged[number] = _with_merge(ctx, number)
        counted = merged.get(number) if type(number) is int else None
        # One result per commit: the reading that arrived last wins.
        at_commit = {}
        for row in rows:
            status = _status(row, files, test)
            if status is not None and row["sha"] in rank and (counted is None or row["sha"] in counted):
                at_commit[row["sha"]] = status
        history = sorted(at_commit.items(), key=lambda item: rank[item[0]])
        if not history:
            continue
        if history[-1][1] == "pass":
            if open_row is not None:
                _clear(store, promises, test, history[-1][0])
                changed = True
            continue
        passes = [index for index, (_, status) in enumerate(history) if status == "pass"]
        start = history[passes[-1] + 1 if passes else 0][0]
        if open_row is not None:
            began = open_row.get("base_sha")
            if began in rank and any(rank[began] < rank[sha] for sha, status in history if status == "pass"):
                _clear(store, promises, test, next(sha for sha, status in history
                                                   if status == "pass" and rank[sha] > rank[began]))
                open_row = None
                changed = True
            elif began in rank and rank[start] < rank[began]:
                _move(store, open_row, test, start)
                changed = True
                continue
            else:
                continue  # Same red run, already reported (a commit no longer listed keeps its report).
            if approvals is None:
                approvals = _approvals(store)
                issues = {row["number"]: row for row in store.local_issues() if type(row.get("number")) is int}
            number = _owner(ctx, test.split("::", 1)[0], approvals, issues, ask_tracker=ask_tracker)
        if number is None:
            continue
        record = orch_inbox.add_request(store, "gate", _text(number, test, start),
                                        {"promise": {"test": test, "issue": number, "base_sha": start}})
        promises[test] = {"issue": number, "request": record["id"], "since": store._timestamp(), "base_sha": start}
        changed = True
    if changed:
        store._write_json(store.root / FILE, {"open": promises})


def _text(number: int, test: str, start: str) -> str:
    return (f"Closed issue #{number}'s locked test {test} fails on main, first measured red at"
            f" {start[:12]}. Its behaviour is broken; order a fix.")


def _clear(store, promises: dict[str, dict[str, Any]], test: str, base: str) -> None:
    row = promises.pop(test)
    append_event(store, store._now(), row["issue"], "promise-cleared",
                 f"#{row['issue']} {test} passes on main again", test=test, base_sha=base,
                 request=row.get("request"))


def _move(store, row: dict[str, Any], test: str, start: str) -> None:
    """A reading of an older commit shows this red run began earlier: the report names it."""
    from foragent import orch_inbox

    row["base_sha"] = start
    if row.get("request"):
        try:
            orch_inbox.annotate_request(store, row["request"], {
                "text": _text(row["issue"], test, start),
                "promise": {"test": test, "issue": row["issue"], "base_sha": start}})
        except Exception:  # noqa: BLE001 - a removed request keeps the promise's own record
            pass
