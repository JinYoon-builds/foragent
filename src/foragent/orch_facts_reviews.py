"""Review rounds, repeated finding classes, spend and scope drift for `fa orch facts`.

Reads the review artifacts the runner saved (``runs/issue-N/review-K.txt``) from the
frozen snapshot. Each artifact belongs to the latest execution of its issue that had
started by the file's modification time. Classes come only from the reviewer's
``class: <slug>`` line under a finding (or a ``class`` field of a JSON finding);
nothing is inferred from the finding's wording.

Spend compares each execution with the ETA's current size basis (progress.merged_samples),
not an estimate stored at approval. Scope drift compares its captured file sets by the
gate's own path rule. Both publish raw values and arithmetic only; unknown stays null.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from glob import escape
import json
from pathlib import Path
import re
from statistics import median
from types import SimpleNamespace
from typing import Any

from foragent import executors, orch_facts_machine, review_notes
from foragent.i18n import text
from foragent.orch_facts_resources import _jsonl, _time
from foragent.run_review import FINDING_LINE, SPEC_CONFLICT, code_review_findings, parse_review
from foragent.state import StateStore
from foragent.watch_sections import progress

UNCLASSIFIED = "unclassified"
TOP_RUNS = 5
CLASS_LINE = re.compile(r"^\s+class:\s*(\S+)\s*$")


def _class(value: Any) -> str:
    return value.strip() if isinstance(value, str) and value.strip() else UNCLASSIFIED


def finding_classes(text: str) -> set[str]:
    """The classes of every finding in one review's text, whatever its verdict.

    Findings are what the runner's /code-review parser finds: path:line lines (each class
    is the indented ``class:`` line right under it), else the JSON finding objects (each
    class is its ``class`` field). A finding without a class is unclassified.
    """
    _, findings, _ = code_review_findings(text)
    if findings and isinstance(findings[0], dict):
        return {_class(item.get("class")) for item in findings}
    lines = text.splitlines()
    classes = set()
    for index, line in enumerate(lines):
        if (line.strip().startswith(SPEC_CONFLICT) or review_notes.note_text(line) is not None
                or not FINDING_LINE.match(line)):
            continue
        match = CLASS_LINE.match(lines[index + 1]) if index + 1 < len(lines) else None
        classes.add(match[1] if match else UNCLASSIFIED)
    return classes


def round_classes(text: str, recorded: str | None = None) -> set[str] | None:
    """The distinct classes of one usable review; None for an unusable one.

    ``recorded`` is the reviewer's verdict the runner journaled for this round. It decides only
    whether the round counts: a timed-out reviewer or a failed panel can leave a VERDICT line in
    the raw text of an UNUSABLE round. A review the journal never recorded (written before
    review events), or a SPEC-CONFLICT round journaled before the reviewer's own verdict was
    kept beside it, is judged by the text protocol and legacy findings format. Findings count
    whatever the verdict; a FAIL with no recognisable finding is one unclassified round.
    """
    if recorded in (None, "SPEC-CONFLICT"):
        recorded = parse_review(text).verdict
        if recorded == "UNUSABLE":
            # These old artifacts predate the verdict contract; current rounds use their journal.
            recorded = code_review_findings(text)[0]
    if recorded not in ("PASS", "FAIL"):
        return None
    classes = finding_classes(text)
    return classes or ({UNCLASSIFIED} if recorded == "FAIL" else set())


TOLERANCE = timedelta(seconds=1)


def _recorded(store: StateStore) -> dict[tuple[int, int], list[tuple[datetime, str]]]:
    """The reviewer's journaled verdict per (issue, round), in time order.

    Only reviewer rounds save a review file. The orchestrator's verdict for an exact head
    reuses the next round number without one, so it is never matched to a file. For a
    SPEC-CONFLICT round the reviewer's own verdict, kept beside it, is the one that counts.
    """
    found: dict[tuple[int, int], list[tuple[datetime, str]]] = {}
    for event in _jsonl(store.root / "orch-events.jsonl"):
        if (event.get("kind") != "review" or type(event.get("issue")) is not int
                or type(event.get("round")) is not int or not isinstance(event.get("verdict"), str)
                or event.get("source", "reviewer") != "reviewer"):
            continue
        verdict = event["verdict"]
        if verdict == "SPEC-CONFLICT" and isinstance(event.get("review_verdict"), str):
            verdict = event["review_verdict"]
        if (at := _time(event.get("at"))) is not None:
            found.setdefault((event["issue"], event["round"]), []).append((at, verdict))
    return {key: sorted(value) for key, value in found.items()}


def _verdict(events: list[tuple[datetime, str]], modified: datetime) -> str | None:
    """The reviewer verdict journaled right after the file was saved."""
    return next((verdict for at, verdict in events if at >= modified - TOLERANCE), None)


def _artifacts(store: StateStore) -> dict[int, list[tuple[datetime, int, set[str]]]]:
    """Usable numbered review files per issue: (modified, round, classes)."""
    recorded = _recorded(store)
    found: dict[int, list[tuple[datetime, int, set[str]]]] = {}
    for path in (store.root / "runs").glob("issue-*/review-*.txt"):
        number, suffix = path.parent.name.removeprefix("issue-"), path.stem.removeprefix("review-")
        if not number.isdigit() or not suffix.isdigit():
            continue
        try:
            modified = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
            verdict = _verdict(recorded.get((int(number), int(suffix)), []), modified)
            classes = round_classes(path.read_text(encoding="utf-8"), verdict)
        except (OSError, UnicodeDecodeError):
            continue
        if classes is not None:
            found.setdefault(int(number), []).append((modified, int(suffix), classes))
    return found


def attach(store: StateStore, executions: list, rows: list[dict[str, Any]],
           merged: set[tuple[int, str]] | None = None) -> dict[str, Any]:
    """Add review_rounds and finding_classes to each row; return the two summaries.

    `merged` holds the (issue, started_at) that merged by store.run_merged; derived when absent."""
    if merged is None:
        from foragent.orch_facts_runs import merged_keys

        merged = merged_keys(store, rows)
    by_issue: dict[int, list[int]] = {}
    for index, execution in enumerate(executions):
        by_issue.setdefault(execution.run.number, []).append(index)
        rows[index]["review_rounds"] = 0
        rows[index]["finding_classes"] = {}
    for number, artifacts in _artifacts(store).items():
        for modified, _, classes in sorted(artifacts, key=lambda item: (item[0], item[1])):
            owners = [index for index in by_issue.get(number, []) if executions[index].started <= modified]
            if not owners:
                continue
            row = rows[owners[-1]]
            row["review_rounds"] += 1
            for slug in sorted(classes):
                row["finding_classes"][slug] = row["finding_classes"].get(slug, 0) + 1
    classes: dict[str, dict[str, Any]] = {}
    for row in rows:
        for slug, rounds in row["finding_classes"].items():
            entry = classes.setdefault(slug, {"rounds": 0, "runs": 0, "issues": set()})
            entry["rounds"] += rounds
            entry["runs"] += 1
            entry["issues"].add(row["issue"])
    finding_classes = {slug: {"rounds": entry["rounds"], "runs": entry["runs"],
                              "repeated_rounds": entry["rounds"] - entry["runs"],
                              "repeated_runs": entry["runs"] - 1, "issues": sorted(entry["issues"])}
                       for slug, entry in sorted(classes.items())}
    merged_rows = [row for row in rows if row["status"] == "finished" and (row["issue"], row["started_at"]) in merged]
    counts = [row["review_rounds"] for row in merged_rows]
    top = sorted(merged_rows, key=lambda row: -row["review_rounds"])[:TOP_RUNS]
    return {
        "review_rounds": {"median": median(counts) if counts else None,
                          "top_runs": [{"issue": row["issue"], "started_at": row["started_at"],
                                        "review_rounds": row["review_rounds"]} for row in top]},
        "finding_classes": finding_classes,
    }


def recorded_rounds(store: StateStore) -> dict[tuple[int, datetime], int]:
    """Usable reviewer rounds per (issue, execution start). No reviewer record at all: absent, so unknown."""
    starts: dict[int, list[datetime]] = {}
    for run in [*store.read_history(), *store.active_runs()]:
        if run.kind == "issue" and (at := _time(run.started_at)) is not None:
            starts.setdefault(run.number, []).append(at)

    def owner(number: int, at: datetime) -> tuple[int, datetime] | None:
        start = max((item for item in starts.get(number, []) if item <= at), default=None)
        return None if start is None else (number, start)

    rounds: dict[tuple[int, datetime], int] = {}
    for (number, _), events in _recorded(store).items():
        for at, _ in events:
            if (key := owner(number, at)) is not None:
                rounds.setdefault(key, 0)
    for number, artifacts in _artifacts(store).items():
        for modified, _, _ in artifacts:
            if (key := owner(number, modified)) is not None:
                rounds[key] = rounds.get(key, 0) + 1
    return rounds


def _ratio(value: float | None, basis: float | None) -> float | None:
    return value / basis if value is not None and basis else None


def _size_basis(stores: list[StateStore], now: datetime) -> dict[str, dict[str, Any]]:
    """Each size's typical seconds and rounds from one population of merged samples."""
    rounds = {}
    for store in stores:
        try:
            rounds[store.root] = recorded_rounds(store)
        except (OSError, ValueError, KeyError, TypeError):
            rounds[store.root] = {}  # As in the ETA: an unreadable repository adds no rounds.
    basis = {}
    for size, samples in progress.merged_samples(stores, now).items():
        _, typical, _, runs = progress.expected([seconds for *_, seconds in samples], size)
        known = [count for store, number, start, _ in samples
                 if (count := rounds[store.root].get((number, start))) is not None]
        enough = len(known) >= progress.LEARN_MIN
        basis[size] = {"typical_seconds": typical, "basis_runs": runs,
                       "typical_rounds": median(known) if enough else None,
                       "rounds_basis_runs": len(known) if enough else None}
    return basis


def _spend(row: dict[str, Any], size: str | None, actual_rounds: int | None, basis: dict[str, dict[str, Any]],
           as_of: str) -> dict[str, Any]:
    typical = basis.get(size) or dict.fromkeys(("typical_seconds", "basis_runs", "typical_rounds", "rounds_basis_runs"))
    return {
        "size": size, "actual_seconds": row["runtime_seconds"], "actual_rounds": actual_rounds, **typical,
        "runtime_ratio": _ratio(row["runtime_seconds"], typical["typical_seconds"]),
        "rounds_ratio": _ratio(actual_rounds, typical["typical_rounds"]), "as_of": as_of,
        "source": None if size is None else "merged_runs" if typical["basis_runs"] is not None else "size_default",
    }


UNMEASURED = dict.fromkeys(("additions", "deletions", "changed_files", "binary_files", "head", "tree", "measured_at"))


def pr_lines(store: StateStore, run: Any) -> dict[str, Any]:
    """The runner's latest line measurement of this execution, with the revision and time it measured.

    No measurement of this execution (none yet, an older execution's, unreadable) is unknown: null, never 0.
    """
    measured = store.pr_lines(run.number, run.started_at) or {}
    return {key: measured.get(key) for key in UNMEASURED}


def loop_text(row: dict[str, Any], language: str) -> str:
    """One execution's size, usable review rounds and PR line counts, for its human line."""
    lines, size = row.get("pr_lines") or UNMEASURED, (row.get("spend") or {}).get("size")
    if type(lines["additions"]) is int and type(lines["deletions"]) is int:
        shown = text("facts_loop_lines", language, added=lines["additions"], deleted=lines["deletions"],
                     tree=(lines["tree"] or "?")[:7])
    else:
        shown = "?"
    return text("facts_loop", language, size=size or "?", rounds=row.get("review_rounds", "?"), lines=shown)


def _scope_drift(run: Any, sensitive: bool | None) -> dict[str, Any]:
    """Captured file sets; a set recorded before capture existed is unknown (null), never compliant."""
    from foragent.overlap_files import any_overlap

    declared = None if run.declared_files is None else list(dict.fromkeys(run.declared_files))
    changed = None if run.changed_files is None else list(dict.fromkeys(run.changed_files))
    outside = None
    if declared is not None and changed is not None:
        ctx = SimpleNamespace(case_sensitive=sensitive)
        outside = [path for path in changed if not any_overlap(ctx, [escape(path)], declared)]
    return {"declared": declared, "changed": changed, "outside_declared": outside}


def _sum(rows: list[dict[str, Any]], key: str) -> float | None:
    values = [row[key] for row in rows if row[key] is not None]
    return sum(values) if values else None


def _paired_ratio(rows: list[dict[str, Any]], value: str, basis: str) -> float | None:
    """Only executions that know both sides, so an unknown side cannot move the ratio."""
    pairs = [row for row in rows if row[value] is not None and row[basis] is not None]
    return _ratio(_sum(pairs, value), _sum(pairs, basis))


def _area(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """One lane's sums, unknown counts and per-execution detail; ``scope_drift`` holds each issue's latest."""
    spends = [row["spend"] for row in rows]
    sums = ("actual_seconds", "typical_seconds", "actual_rounds", "typical_rounds")
    sources: dict[str, int] = {}
    for spend in spends:
        if spend["source"] is not None:
            sources[spend["source"]] = sources.get(spend["source"], 0) + 1
    return {
        "executions": len(rows),
        "spend": {**{key: _sum(spends, key) for key in sums},
                  "runtime_ratio": _paired_ratio(spends, "actual_seconds", "typical_seconds"),
                  "rounds_ratio": _paired_ratio(spends, "actual_rounds", "typical_rounds"),
                  "unknown": {key: sum(spend[key] is None for spend in spends) for key in sums},
                  "as_of": spends[-1]["as_of"], "sources": dict(sorted(sources.items()))},
        "scope_drift": {str(row["issue"]): row["scope_drift"] for row in rows},
        "scope_drift_unknown": sum(row["scope_drift"]["outside_declared"] is None for row in rows),
        "runs": [{"issue": row["issue"], "started_at": row["started_at"], "spend": row["spend"],
                  "scope_drift": row["scope_drift"]} for row in rows],
    }


def attach_project_review(store: StateStore, root: Any, executions: list, rows: list[dict[str, Any]],
                          machine: dict[str, Any]) -> dict[str, Any]:
    """Add spend and scope_drift to each row; return the lane-grouped areas. Arithmetic only."""
    from foragent import scheduler

    now = machine["now"]
    basis = _size_basis([store, *orch_facts_machine.merge_stores(machine, now)], now)
    rounds = recorded_rounds(store)
    issues = {issue["number"]: issue for issue in store.local_issues()}
    sensitive = scheduler._filesystem_case_sensitive(Path(root))
    areas: dict[str, list[dict[str, Any]]] = {}
    for execution, row in zip(executions, rows):
        run = execution.run
        issue = issues.get(run.number)
        # The ETA's size rule; an issue with no local record has no known size.
        size = None if issue is None else ((issue.get("run_sizes") or {}).get(run.started_at)
                                           or progress.size_of(issue.get("labels", ())))
        row["spend"] = _spend(row, size, rounds.get((run.number, execution.started)), basis, now.isoformat())
        row["scope_drift"] = _scope_drift(run, sensitive)
        row["pr_lines"] = pr_lines(store, run)
        areas.setdefault(row["lane"] or "main", []).append(row)
    return {lane: _area(items) for lane, items in sorted(areas.items())}


def project_review_lines(facts: dict[str, Any], language: str, items: int) -> list[str]:
    """Spend per lane against its size basis, and the executions with files outside their declared set."""
    unknown, days = text("facts_resources_unknown", language), facts["window_days"]

    def shown(value: float | None) -> str:
        return unknown if value is None else f"{value:.0f}"

    areas = sorted(facts["areas"].items(), key=lambda item: -(item[1]["spend"]["actual_seconds"] or 0))
    lanes = [text("facts_spend_lane", language, lane=lane, actual=shown(area["spend"]["actual_seconds"]),
                  typical=shown(area["spend"]["typical_seconds"]), rounds=shown(area["spend"]["actual_rounds"]),
                  typical_rounds=shown(area["spend"]["typical_rounds"])) for lane, area in areas[:items]]
    drifted = [run for _, area in areas for run in area["runs"] if run["scope_drift"]["outside_declared"]]
    listed = ", ".join(f"#{run['issue']} {run['scope_drift']['outside_declared'][0]}" for run in drifted[:items])
    return [text("facts_spend", language, days=days, lanes="; ".join(lanes) or text("facts_schedule_unknown", language)),
            text("facts_scope", language, days=days, count=len(drifted), runs=listed or text("facts_schedule_unknown", language),
                 unknown=sum(area["scope_drift_unknown"] for area in facts["areas"].values()))]


def most_repeated(finding_classes: dict[str, dict[str, Any]]) -> tuple[str, dict[str, Any]] | None:
    """The class repeated most often, within runs and across runs together."""
    if not finding_classes:
        return None
    return min(finding_classes.items(), key=lambda item: (
        -(item[1]["repeated_rounds"] + item[1]["repeated_runs"]), -item[1]["rounds"], item[0]))


def human_lines(facts: dict[str, Any], language: str, items: int) -> list[str]:
    """The run with the most review rounds and the most repeated finding class."""
    summary, days = facts["review_rounds"], facts["window_days"]
    if not summary["top_runs"]:
        lines = [text("facts_review_rounds_none", language, days=days)]
    else:
        worst = summary["top_runs"][0]
        lines = [text("facts_review_rounds", language, days=days, median=f"{summary['median']:g}",
                      issue=worst["issue"], rounds=worst["review_rounds"])]
    if (top := most_repeated(facts["finding_classes"])) is not None:
        slug, entry = top
        issues = ", ".join(f"#{n}" for n in entry["issues"][:items])
        if len(entry["issues"]) > items:
            issues += ", ..."
        lines.append(text("facts_repeated_class", language, slug=slug[:40], within=entry["repeated_rounds"],
                          across=entry["repeated_runs"], issues=issues))
    return lines


# Claude dollar cost of recent issue work (#2730), read from the logs the runner already keeps.
COST_FILE = "claude-costs.json"
# An implementation session's stream (`issue-N.stdout`) or a panel reviewer's document
# (`issue-N-review-R-K.stdout`). Role, docs, dream and draft sessions are not issue work.
COST_LOG = re.compile(r"^issue-(\d+)(?:-review-\d+-\d+)?\.stdout$")
REVIEW_HEAVY = 10
COST_TAIL = 1 << 18
COST_EVIDENCE = ("total_cost_usd in retained Claude issue logs (implementation result events and panel "
                 "reviewer documents); a log is overwritten by the next session of the same name, so this is "
                 "not a lifetime or cross-provider total")


def log_cost(path: Path) -> float | None:
    """Claude's reported cost in one log: its single review document, else its last result event.

    Only the tail is read: the result event ends a session's stream. A log with no readable
    result (a Codex log, an interrupted session) reports nothing, never $0.
    """
    with path.open("rb") as stream:
        size = stream.seek(0, 2)
        stream.seek(max(0, size - COST_TAIL))
        tail = stream.read().decode("utf-8", errors="replace")
    if size <= COST_TAIL:
        try:
            document = json.loads(tail)
        except ValueError:
            document = None
        if isinstance(document, dict):
            return executors.claude_cost(document)
    lines = tail.splitlines()[1 if size > COST_TAIL else 0:]
    for line in reversed(lines):
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get("type") == "result":
            return executors.claude_cost(event)
    return None


def freeze_costs(source: Path, target: Path, since: datetime) -> None:
    """Record each recent issue log's reported cost beside the frozen records (logs are not copied)."""
    records = []
    try:
        paths = sorted((source / "logs").iterdir())
    except OSError:
        paths = []
    for path in paths:
        if (match := COST_LOG.match(path.name)) is None:
            continue
        try:
            modified = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
            if modified < since:
                continue
            usd = log_cost(path)
        except OSError:
            continue
        records.append({"log": path.name, "issue": int(match[1]), "modified": modified.isoformat(), "usd": usd})
    target.mkdir(parents=True, exist_ok=True)
    (target / COST_FILE).write_text(json.dumps(records), encoding="utf-8")


def _cost_records(store: StateStore) -> list[dict[str, Any]]:
    try:
        records = json.loads((store.root / COST_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [record for record in records if isinstance(record, dict) and type(record.get("issue")) is int
            and _time(record.get("modified")) is not None] if isinstance(records, list) else []


def _group(entries: list[dict[str, Any]], total: float) -> dict[str, Any]:
    usd = sum(entry["usd"] for entry in entries)
    return {"usd": usd, "share": usd / total if total else None, "issues": [entry["issue"] for entry in entries]}


def claude_cost(store: StateStore, every: list, rows: list[dict[str, Any]], now: datetime,
                window: timedelta) -> dict[str, Any]:
    """Recent issue-attributed Claude cost by merged state and review rounds. Arithmetic only.

    Each retained log counts once, for its issue, when it was written inside the window. An
    issue is merged when any recorded execution of it merged, unmerged when it has recorded
    executions and none merged, and of unknown merge state with no recorded execution. Review
    rounds are the usable reviewer rounds of the issue's recent executions. A finished recent
    execution with no costed log of its own is disclosed as missing cost, never counted as $0.
    """
    from foragent.orch_facts_runs import merged_keys

    since = now - window
    starts: dict[int, list[datetime]] = {}
    for execution in every:
        starts.setdefault(execution.run.number, []).append(execution.started)
    merged = merged_keys(store, [{"issue": e.run.number, "started_at": e.run.started_at, "outcome": e.run.outcome}
                                 for e in every])
    merged_issues = {number for number, _ in merged}
    costs: dict[int, float] = {}
    costed: set[tuple[int, datetime | None]] = set()
    for record in _cost_records(store):
        number, modified = record["issue"], _time(record["modified"])
        if modified < since or modified > now:
            continue
        usd = record.get("usd")
        if type(usd) not in (int, float) or usd < 0:
            continue
        costs[number] = costs.get(number, 0.0) + float(usd)
        costed.add((number, max((at for at in starts.get(number, []) if at <= modified), default=None)))
    rounds: dict[int, int] = {}
    missing: set[int] = set()
    for row in rows:
        rounds[row["issue"]] = rounds.get(row["issue"], 0) + (row.get("review_rounds") or 0)
        if row["status"] == "finished" and (row["issue"], _time(row["started_at"])) not in costed:
            missing.add(row["issue"])
    issues = []
    for number in sorted(costs.keys() | rounds.keys()):
        state = (number in merged_issues) if number in starts else None
        issues.append({"issue": number, "usd": costs.get(number), "merged": state, "review_rounds": rounds.get(number)})
    known = [entry for entry in issues if entry["usd"] is not None]
    total = sum(entry["usd"] for entry in known)
    heavy = [entry for entry in known if (entry["review_rounds"] or 0) >= REVIEW_HEAVY]
    unknown = [entry for entry in known if entry["merged"] is None]
    return {
        "scope": {"provider": "claude", "evidence": COST_EVIDENCE, "window_days": window.days},
        "total_usd": total,
        "issues": issues,
        "merged": _group([entry for entry in known if entry["merged"] is True], total),
        "unmerged": _group([entry for entry in known if entry["merged"] is False], total),
        "review_heavy": {"min_rounds": REVIEW_HEAVY, **_group(heavy, total)},
        "missing_cost": {"issues": sorted(missing)},
        "unknown_merge": {"usd": sum(entry["usd"] for entry in unknown), "issues": [entry["issue"] for entry in unknown]},
    }


def _usd(value: float) -> str:
    return f"${value:.2f}"


def cost_lines(facts: dict[str, Any], language: str, items: int) -> list[str]:
    """The Claude cost total with its merged/unmerged/review-heavy shares, then the costliest issues."""
    cost = facts["claude_cost"]

    def share(value: float | None) -> str:
        return "?" if value is None else f"{value * 100:.1f}%"

    lines = [text("facts_claude_cost", language, days=cost["scope"]["window_days"], total=_usd(cost["total_usd"]),
                  merged=_usd(cost["merged"]["usd"]), merged_share=share(cost["merged"]["share"]),
                  unmerged=_usd(cost["unmerged"]["usd"]), unmerged_share=share(cost["unmerged"]["share"]),
                  unknown=_usd(cost["unknown_merge"]["usd"]), rounds=cost["review_heavy"]["min_rounds"],
                  heavy=_usd(cost["review_heavy"]["usd"]), heavy_share=share(cost["review_heavy"]["share"]))]
    known = sorted((entry for entry in cost["issues"] if entry["usd"] is not None), key=lambda entry: -entry["usd"])
    missing = cost["missing_cost"]["issues"]
    if known or missing:
        shown = ", ".join(f"#{entry['issue']} {_usd(entry['usd'])}" for entry in known[:items])
        if len(known) > items:
            shown += ", ..."
        listed = ", ".join(f"#{number}" for number in missing[:items]) + (", ..." if len(missing) > items else "")
        lines.append(text("facts_claude_cost_issues", language,
                          issues=shown or text("facts_schedule_unknown", language),
                          missing=listed or text("facts_schedule_unknown", language)))
    return lines
