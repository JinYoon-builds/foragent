"""Closed issues' locked tests stay green on main (D122, #2925).

Main's acceptance results reach this module two ways: every comparison that
reads CI acceptance reports (run, merge owner, merge batch) passes the reports
it accepted to ``observe``, and each gate tick calls ``observe_baselines``,
which reads the gate's own baseline captures (the base the runner's
comparisons read). No test run is started here.

Each reading is kept as a small digest in ``promise-readings.json``: the
acceptance tests it collected (a bitmap over one shared ``ids`` table), the
ones that failed, and the ones whose result says nothing (skipped, xfailed; a
CI report cannot tell those from failures, so its non-passing tests say
nothing and failures come only from captures' JUnit results). A collected test
in neither list passed; a test the reading did not
collect says nothing, so a partial capture hides no other capture's failure.
A baseline capture is digested once per file version, whatever order captures
finish in. There is no read cursor: every evaluation reads all kept readings.

Each test is judged by main's latest measurement of it: readings are placed in
main's commit order, and at one commit the reading measured last (a capture's
``created_at``, a CI comparison's observation time) wins. A failure measured
while the test's issue was seen open never counts (``open_seen``). A locked acceptance test of a closed issue whose latest measurement
fails is reported once to the orchestrator request box, naming the first commit
of its current red run and the latest measured one. It stays open in
``promises.json`` until a later measurement passes, which appends one
``promise-cleared`` event; a later red run is a new report. A report whose
commit no reading holds (an older alarm's record) ends at any pass measured
after it. A reading that shows the red run began earlier moves the open
report's commit back. This module is the only writer of both files, under
``promises.lock``; the /fa report reads ``promises.json``.
"""

from __future__ import annotations

from base64 import b64decode, b64encode
from datetime import datetime, timezone
from functools import cmp_to_key
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
    with _file_lock(ctx.store.root / "promises.lock"):
        readings = _load(ctx.store, READINGS)
        # A CI report lists collected and passing ids only: a non-passing test may have failed or been
        # skipped, so CI is evidence of passes; failures come from captures' JUnit results.
        digest = _digest(ctx, readings, base, collected, passing)
        ci = [row for row in readings.get("ci", []) if isinstance(row, dict) and "collected" in row]
        digest["seq"] = _next_seq(readings)
        readings["ci"] = ([digest] + ci)[:KEEP_CI]
        _keep_ids(readings)
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
        if not isinstance(readings.get("ids"), list):
            known = {}  # Digests from before per-test collected ids are read again once.
        kept, changed = {}, False
        for path in sorted(cache.glob("*.json")):
            try:
                stat = path.stat()
            except OSError:
                continue
            version = f"{stat.st_mtime_ns}:{stat.st_size}"
            old = known.get(path.stem)
            if isinstance(old, dict) and old.get("version") == version and ("sha" not in old or "collected" in old):
                kept[path.stem] = old
                continue
            changed = True
            # Every capture file not yet digested is read, whatever its time stamp says.
            entry = {"version": version}
            try:
                saved = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(saved, dict) and belongs(saved, path, ctx.repo_root, ctx.config.repo):
                    entry |= _baseline_digest(ctx, readings, saved) or {}
            except (OSError, ValueError, TypeError):
                pass
            if "sha" in entry:
                entry["seq"] = _next_seq(readings)
            kept[path.stem] = entry
        changed |= set(known) != set(kept)
        readings["baselines"] = kept
        if changed:
            _keep_ids(readings)
            store._write_json(store.root / READINGS, readings)
        _evaluate(ctx, readings, ask_tracker=False)


def _baseline_digest(ctx, readings, saved: dict[str, Any]) -> dict[str, Any] | None:
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
    measured = _when(saved.get("created_at"))
    return _digest(ctx, readings, base, collected, set(passing), failing=failing,
                   measured=measured.isoformat() if measured else None)


def _digest(ctx, readings, base: str, collected: set[str], passing: set[str], *, failing: set[str] = frozenset(),
            measured: str | None = None) -> dict[str, Any]:
    """One reading: the acceptance tests it collected (a bitmap over ``ids``), failed, or says nothing of."""
    acceptance = ctx.config.acceptance_dir.rstrip("/") + "/"
    collected = {test for test in collected if isinstance(test, str) and test.startswith(acceptance)}
    failing = (set(failing) & collected) - passing
    at = ctx.store._timestamp()
    return {"sha": base, "at": at, "measured": measured or at, "failing": sorted(failing),
            "unknown": sorted(collected - passing - failing),
            "collected": _encode(_indices(readings, collected))}


def _when(value) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment.astimezone(timezone.utc) if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _indices(readings: dict[str, Any], tests: set[str]) -> set[int]:
    ids = readings.setdefault("ids", [])
    if not isinstance(ids, list):
        ids = readings["ids"] = []
    position = {test: index for index, test in enumerate(ids)}
    for test in sorted(tests - set(position)):
        position[test] = len(ids)
        ids.append(test)
    return {position[test] for test in tests}


def _encode(indices: set[int]) -> str:
    mask = sum(1 << index for index in indices)
    return b64encode(mask.to_bytes((mask.bit_length() + 7) // 8, "little")).decode()


def _decode(value) -> int:
    try:
        return int.from_bytes(b64decode(value), "little") if isinstance(value, str) else 0
    except ValueError:
        return 0


def _next_seq(readings: dict[str, Any]) -> int:
    seq = readings.get("seq") if type(readings.get("seq")) is int else 0
    readings["seq"] = seq + 1
    return seq + 1


def _keep_ids(readings: dict[str, Any]) -> None:
    """Keep only the test ids some kept reading collected, re-indexing every reading's bitmap."""
    readings.pop("file_sets", None)
    ids = readings.get("ids") if isinstance(readings.get("ids"), list) else []
    rows = _rows(readings)
    masks = [_decode(row.get("collected")) for row in rows]
    used = 0
    for mask in masks:
        used |= mask
    keep = [index for index in range(len(ids)) if used >> index & 1]
    if len(keep) == len(ids):
        return
    moved = {old: new for new, old in enumerate(keep)}
    for row, mask in zip(rows, masks):
        row["collected"] = _encode({moved[index] for index in range(mask.bit_length()) if mask >> index & 1})
    readings["ids"] = [ids[index] for index in keep]


def _rows(readings: dict[str, Any]) -> list[dict[str, Any]]:
    baselines = readings.get("baselines") if isinstance(readings.get("baselines"), dict) else {}
    rows = [row for row in readings.get("ci", []) if isinstance(row, dict)]
    return rows + [row for row in baselines.values() if isinstance(row, dict)]


def _readings(readings: dict[str, Any]) -> list[dict[str, Any]]:
    """Every usable reading, oldest measurement first, with its sets decoded once."""
    baselines = readings.get("baselines") if isinstance(readings.get("baselines"), dict) else {}
    out = []
    for row in _rows(readings):
        if not isinstance(row.get("sha"), str) or type(row.get("seq")) is not int or "collected" not in row:
            continue
        measured = _when(row.get("measured")) or _when(row.get("at"))
        if measured is None:
            continue
        out.append({"sha": row["sha"], "seq": row["seq"], "measured": measured,
                    "mask": _decode(row["collected"]), "failing": set(row.get("failing", ())),
                    "unknown": set(row.get("unknown", ()))})
    return sorted(out, key=lambda row: (row["measured"], row["seq"]))


def _status(row: dict[str, Any], index: dict[str, int], test: str) -> str | None:
    """pass, fail, or None when the reading says nothing of the test (not collected, skipped, xfailed)."""
    if test in row["failing"]:
        return "fail"
    if test in row["unknown"]:
        return None
    position = index.get(test)
    return "pass" if position is not None and row["mask"] >> position & 1 else None


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


def _history(rows, index, rank, test: str, open_until: datetime | None):
    """Main's result for ``test`` at each measured commit, in commit order, plus its passes' times.

    At one commit the latest measurement that says anything wins. A failure measured while the
    test's issue was seen open never counts.
    """
    at_commit, passes = {}, []
    for row in rows:
        status = _status(row, index, test)
        if status is None or row["sha"] not in rank:
            continue
        if status == "fail" and open_until is not None and row["measured"] <= open_until:
            continue
        at_commit[row["sha"]] = status
        if status == "pass":
            passes.append(row["measured"])
    return sorted(at_commit.items(), key=lambda item: rank[item[0]]), passes


def _evaluate(ctx, readings: dict[str, Any], *, ask_tracker: bool) -> None:
    """Bring ``promises.json`` in line with every kept reading: each test by main's latest measurement."""
    from foragent import orch_inbox

    store = ctx.store
    saved = _saved(store)
    promises = open_promises(store)
    seen = saved.get("open_seen") if isinstance(saved.get("open_seen"), dict) else {}
    seen = {key: value for key, value in seen.items() if _when(value) is not None}
    rows = _readings(readings)
    oldest = rows[0]["measured"] if rows else None
    changed = False
    # An "issue seen open" time only matters while a reading measured before it is kept.
    for key in [key for key, value in seen.items() if oldest is None or _when(value) < oldest]:
        seen.pop(key)
        changed = True
    candidates = {test for row in rows for test in row["failing"]} | set(promises)
    approvals, issues, owners = None, None, {}
    now = store._timestamp()

    def owner_states(path: str) -> list[tuple[int, str | None]]:
        nonlocal approvals, issues
        if path not in owners:
            if approvals is None:
                approvals = _approvals(store)
                issues = {row["number"]: row for row in store.local_issues() if type(row.get("number")) is int}
            numbers = sorted(number for number, files in approvals.items() if path in files)
            owners[path] = [(number, _state(ctx, number, issues, ask_tracker)) for number in numbers]
        return owners[path]

    tests = set(promises)
    for test in sorted(candidates):
        states = owner_states(test.split("::", 1)[0])
        for number, state in states:
            if state == "open" and seen.get(str(number)) != now:
                seen[str(number)] = now  # Its locks fail by design until it ships.
                changed = True
        if states and all(state == "closed" for _, state in states):
            tests.add(test)
    index = {test: position for position, test in enumerate(readings.get("ids", []))}
    first_seq = {}
    for row in sorted(rows, key=lambda row: row["seq"]):
        first_seq.setdefault(row["sha"], row["seq"])
    rank = _order(ctx, set(first_seq), first_seq) if tests else {}
    for test in sorted(tests):
        states = owner_states(test.split("::", 1)[0])
        opened = [_when(seen.get(str(number))) for number, _ in states if seen.get(str(number))]
        history, passes = _history(rows, index, rank, test, max(opened) if opened else None)
        if not history:
            continue
        open_row = promises.get(test)
        latest = history[-1][0]
        if history[-1][1] == "pass":
            if open_row is not None:
                _clear(store, promises, test, latest)
                changed = True
            continue
        passed = [position for position, (_, status) in enumerate(history) if status == "pass"]
        start = history[passed[-1] + 1 if passed else 0][0]
        if open_row is not None:
            began = open_row.get("base_sha")
            since = _when(open_row.get("since"))
            if began in rank and any(rank[began] < rank[sha] for sha, status in history if status == "pass"):
                _clear(store, promises, test, next(sha for sha, status in history
                                                   if status == "pass" and rank[sha] > rank[began]))
                open_row = None
                changed = True
            elif began not in rank and since is not None and any(moment > since for moment in passes):
                # A report whose commit no reading holds (older alarm, evicted reading) ends at a later pass.
                _clear(store, promises, test, history[passed[-1]][0] if passed else start)
                open_row = None
                changed = True
            elif began in rank and rank[start] < rank[began]:
                _move(store, open_row, test, start, latest)
                changed = True
                continue
            else:
                continue  # Same red run, already reported.
        if not states or not all(state == "closed" for _, state in states):
            continue
        number = states[-1][0]
        record = orch_inbox.add_request(store, "gate", _text(number, test, start, latest),
                                        {"promise": {"test": test, "issue": number, "base_sha": start,
                                                     "latest_sha": latest}})
        promises[test] = {"issue": number, "request": record["id"], "since": now, "base_sha": start}
        changed = True
    if changed:
        store._write_json(store.root / FILE, {"open": promises, "open_seen": seen})


def _text(number: int, test: str, start: str, latest: str) -> str:
    latest_part = "" if latest == start else f", latest measured red at {latest[:12]}"
    return (f"Closed issue #{number}'s locked test {test} fails on main, first measured red at"
            f" {start[:12]}{latest_part}. Its behaviour is broken; order a fix.")


def _clear(store, promises: dict[str, dict[str, Any]], test: str, base: str) -> None:
    row = promises.pop(test)
    append_event(store, store._now(), row["issue"], "promise-cleared",
                 f"#{row['issue']} {test} passes on main again", test=test, base_sha=base,
                 request=row.get("request"))


def _move(store, row: dict[str, Any], test: str, start: str, latest: str) -> None:
    """A reading of an older commit shows this red run began earlier: the report names it."""
    from foragent import orch_inbox

    row["base_sha"] = start
    if row.get("request"):
        try:
            orch_inbox.annotate_request(store, row["request"], {
                "text": _text(row["issue"], test, start, latest),
                "promise": {"test": test, "issue": row["issue"], "base_sha": start, "latest_sha": latest}})
        except Exception:  # noqa: BLE001 - a removed request keeps the promise's own record
            pass
