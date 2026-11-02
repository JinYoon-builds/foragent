"""Review rounds and repeated finding classes for `fa orch facts`.

Reads the review artifacts the runner saved (``runs/issue-N/review-K.txt``) from the
frozen snapshot. Each artifact belongs to the latest execution of its issue that had
started by the file's modification time. Classes come only from the reviewer's
``class: <slug>`` line under a finding (or a ``class`` field of a JSON finding);
nothing is inferred from the finding's wording.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import re
from statistics import median
from typing import Any

from foragent import review_notes
from foragent.i18n import text
from foragent.orch_facts_resources import _jsonl, _time
from foragent.run_review import FINDING_LINE, SPEC_CONFLICT, code_review_findings, parse_review
from foragent.state import StateStore

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


def attach(store: StateStore, executions: list, rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Add review_rounds and finding_classes to each row; return the two summaries."""
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
    merged = [row for row in rows if row["status"] == "finished" and row["outcome"] == "merged"]
    counts = [row["review_rounds"] for row in merged]
    top = sorted(merged, key=lambda row: -row["review_rounds"])[:TOP_RUNS]
    return {
        "review_rounds": {"median": median(counts) if counts else None,
                          "top_runs": [{"issue": row["issue"], "started_at": row["started_at"],
                                        "review_rounds": row["review_rounds"]} for row in top]},
        "finding_classes": finding_classes,
    }


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
