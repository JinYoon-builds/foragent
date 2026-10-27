"""Review rounds and repeated finding classes for `fa orch facts`.

Reads the review artifacts the runner saved (``runs/issue-N/review-K.txt``) from the
frozen snapshot. Each artifact belongs to the latest execution of its issue that had
started by the file's modification time. Classes come only from the reviewer's
``class: <slug>`` line under a finding (or a ``class`` field of a JSON finding);
nothing is inferred from the finding's wording.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
import re
from statistics import median
from typing import Any

from foragent.orch_facts_resources import _jsonl, _time
from foragent.run_review import SPEC_CONFLICT, parse_code_review, parse_review
from foragent.state import StateStore

UNCLASSIFIED = "unclassified"
TOP_RUNS = 5
# The finding pattern parse_code_review uses.
FINDING = re.compile(r"^(?:[-*]|\d+[.)])?\s*`?\S+?:\d+`?\s+(—|-|--)\s+")
CLASS_LINE = re.compile(r"^\s+class:\s*(\S+)\s*$")


def _line_classes(text: str) -> set[str]:
    """Classes of the path:line findings in text; a finding without a class line is unclassified."""
    lines = text.splitlines()
    classes = set()
    for index, line in enumerate(lines):
        if line.strip().startswith(SPEC_CONFLICT) or not FINDING.match(line):
            continue
        following = lines[index + 1] if index + 1 < len(lines) else ""
        match = CLASS_LINE.match(following)
        classes.add(match[1] if match else UNCLASSIFIED)
    return classes


def _json_items(text: str) -> list[Any] | None:
    payload = text.strip()
    blocks = list(re.finditer(r"^[ \t]*```([^\r\n]*)\r?\n(.*?)^[ \t]*```[ \t]*\r?$", payload, re.M | re.S))
    json_blocks = [block for block in blocks if block[1].strip() == "json"]
    if len(blocks) == 1 and blocks[0].span() == (0, len(payload)):
        payload = blocks[0][2]
    elif json_blocks:
        payload = json_blocks[0][2]
    try:
        items = json.loads(payload)
    except ValueError:
        return None
    return items if isinstance(items, list) else None


def round_classes(text: str, recorded: str | None = None) -> set[str] | None:
    """The distinct classes of one review: None for an unusable review, empty for PASS.

    ``recorded`` is the reviewer's verdict the runner journaled for this round. It decides: a
    timed-out reviewer or a failed panel can leave a VERDICT line in the raw text of an UNUSABLE
    round. A review the journal never recorded (written before review events), or a
    SPEC-CONFLICT round journaled before the reviewer's own verdict was kept beside it, is
    judged by parsing the text the same way the runner did.
    """
    if recorded in (None, "SPEC-CONFLICT"):
        review = parse_review(text)
        if review.verdict == "UNUSABLE":
            review = parse_code_review(json.dumps({"result": text}))
        recorded = review.verdict
    if recorded == "FAIL":
        if parse_review(text).verdict == "UNUSABLE" and (items := _json_items(text)) is not None:
            classes = {item["class"] if isinstance(item, dict) and isinstance(item.get("class"), str)
                       and item["class"].strip() else UNCLASSIFIED for item in items}
        else:
            classes = _line_classes(text)
        return classes or {UNCLASSIFIED}
    if recorded == "PASS":
        return set()
    return None


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
