"""Expected change size beside the preread design, and its comparison after merge (#1696).

The worker reports ``SIZE: source=<n> tests=<n> files=<n>``. The estimate is recorded with
the preread answer but never enters the evidence key, so earlier clean prereads stay usable.
A split suggestion is derived from the estimate and the current ``size_limits`` each time it
is shown; it informs the person and never refuses publication or approval. After a merge,
one row with the expected and the measured size is appended to ``sizes.jsonl``.
"""

from datetime import UTC, datetime
import json
import os
from pathlib import Path
import re

from foragent.i18n import text

COUNTS = ("source_lines", "test_lines", "file_count")
JOURNAL = "sizes.jsonl"
# Sent to the worker only; it is not part of the preread prompt the evidence key binds.
PROMPT = ("\n\nAlso add exactly one line with the change size you expect for the whole issue: "
          "SIZE: source=<n> tests=<n> files=<n>\n"
          "source and tests are added plus deleted lines under src/ and tests/; files counts every "
          "changed path. Give your best whole-number estimate; the line informs the orderer and never "
          "blocks approval.\n")
_LINE = re.compile(r"(?i)^(?:[-*•]|\d+[.)])?\s*SIZE:")
_VALUE = re.compile(r"(?i)^(?:[-*•]|\d+[.)])?\s*SIZE:\s*source=(\d+)\s+tests=(\d+)\s+files=(\d+)\s*$")


def parse(message: str) -> dict | None:
    """The last well-formed SIZE line; a missing or malformed one is unknown (None), never zero."""
    size = None
    for line in message.splitlines():
        match = _VALUE.match(line.strip())
        if match:
            size = dict(zip(COUNTS, map(int, match.groups())))
    return size


def without(message: str) -> str:
    """The answer without SIZE lines, so questions and approval read exactly as before."""
    return "\n".join(line for line in message.splitlines() if not _LINE.match(line.strip()))


def over(size: dict | None, label: str | None, limits: dict) -> list[str]:
    """Each count strictly above the label's limit, as ``name value > limit``."""
    limit = limits.get(label) if isinstance(limits, dict) else None
    if not isinstance(size, dict) or not isinstance(limit, dict):
        return []
    return [f"{name} {size[name]} > {limit[name]}" for name in COUNTS
            if isinstance(size.get(name), int) and isinstance(limit.get(name), int) and size[name] > limit[name]]


def row(size: dict | None, label: str | None, config) -> dict:
    """The preread JSON fields: the estimate when known, and a split note above a limit."""
    if size is None:
        return {}
    above = over(size, label, config.size_limits)
    return {"expected_size": size} | (
        {"size_note": text("size_split", config.language, label=label, counts="; ".join(above))} if above else {})


def lines(result: dict, language: str) -> list[str]:
    """The text view of one preread row."""
    size = result.get("expected_size")
    if size is None:
        return [text("size_unknown", language)]
    shown = [text("size_known", language, **size)]
    return shown + ([f"  {result['size_note']}"] if "size_note" in result else [])


def measure(git, base: str, head_sha: str) -> dict:
    """Added plus deleted lines under src/ and tests/ and every changed path, from the merge base."""
    size = dict.fromkeys(COUNTS, 0)
    for entry in git._call(["diff", "--numstat", "--no-renames", "-z", base, head_sha]).split("\0"):
        if not entry.strip():
            continue
        added, deleted, path = entry.split("\t", 2)
        count = sum(int(value) for value in (added, deleted) if value.isdigit())
        size["file_count"] += 1
        if path.startswith("src/"):
            size["source_lines"] += count
        elif path.startswith("tests/"):
            size["test_lines"] += count
    return size


ESTIMATES = "size-estimates.jsonl"
_KEPT = ("key", "plan", "content", "files", "size")


def _append(root: Path, journal: str, entry: dict) -> None:
    """One O_APPEND write; the leading newline isolates an interrupted tail."""
    payload = ("\n" + json.dumps(entry, ensure_ascii=False) + "\n").encode("utf-8")
    root.mkdir(parents=True, exist_ok=True)
    fd = os.open(root / journal, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    try:
        os.write(fd, payload)
        os.fsync(fd)
    finally:
        os.close(fd)


def _entries(root: Path, journal: str) -> list[dict]:
    try:
        content = (root / journal).read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    entries = []
    for line in content.splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue  # an interrupted tail
        if isinstance(entry, dict):
            entries.append(entry)
    return entries


def retain(plan: dict, root: Path, config, store, by_number: dict) -> None:
    """After a successful publication, keep the estimate of each published issue's exact preread.

    The record is the one whose evidence key is this published plan (references included), so a
    later reread of changed content or a publication that failed never replaces it. Approval by
    ``--approve`` or later by label reads the same kept record. A failure here leaves the merge
    comparison unknown and never fails the publication.
    """
    from foragent import order

    try:
        records = order._preread_records(root)
        items = [item for item in plan["issues"] if "statements" not in item]
        items += [order.edit_view(item, by_number[item["number"]], config) for item in plan.get("edits", [])
                  if "statements" not in item and item["number"] in by_number]
        waiting, _ = order._uncreated_lanes(plan, config, items)
        for item in items:
            if item["key"] in waiting:
                continue  # no implementer read it before its lane existed
            record = records.get(order.preread_target(plan, item, config, root)[2])
            if isinstance(record, dict):
                _append(store.root, ESTIMATES, {name: record.get(name) for name in _KEPT}
                        | {"at": datetime.now(UTC).isoformat(timespec="seconds")})
    except (OSError, ValueError, order.PlanError):
        pass


def record(n: int, ctx, git, approval, base_sha: str, head_sha: str, pr: int, log=None) -> None:
    """Retain one merged comparison; a failure is logged and never stops the merge."""
    from foragent.order import latest_preread

    try:
        base = git._call(["merge-base", base_sha, head_sha]).strip()
        # The estimate is the one kept when this body was published (``retain``), matched by the
        # approved body and snapshot; no preread made later and no file state is consulted.
        latest = None if approval is None else latest_preread(
            ctx.repo_root, n, approval.body, approval.acceptance_blobs or {}, None,
            records=_entries(ctx.store.root, ESTIMATES))
        expected = (latest or {}).get("size")
        _append(ctx.store.root, JOURNAL, {
            "issue": n, "pr": pr, "expected_size": expected if isinstance(expected, dict) else None,
            "actual_size": measure(git, base, head_sha), "head": head_sha,
            "at": datetime.now(UTC).isoformat(timespec="seconds")})
    except Exception as exc:  # The merge already happened; the comparison is only a record of it.
        if log is not None:
            log(f"Size record skipped: {exc}")


def report(root: Path) -> dict:
    """One row per issue and PR (the last write wins), and the share of wrong known estimates."""
    rows: dict[tuple, dict] = {}
    for entry in _entries(root, JOURNAL):
        if "issue" in entry:
            rows[(entry.get("issue"), entry.get("pr"))] = entry
    records = list(rows.values())
    known = [r for r in records if isinstance(r.get("expected_size"), dict) and isinstance(r.get("actual_size"), dict)]
    wrong = sum(1 for r in known if any(r["expected_size"].get(name) != r["actual_size"].get(name) for name in COUNTS))
    return {"records": records, "compared": len(known), "wrong": wrong, "unknown": len(records) - len(known),
            "wrong_share": wrong / len(known) if known else None}


def run(args, root: Path) -> int:
    """`fa order sizes`: retained comparisons and the share of wrong estimates."""
    from foragent.commands import facade, repo_store

    cli = facade()
    config, _repo, store = repo_store(root)
    result = report(store.root)
    if args.json:
        print(cli.json.dumps(result, ensure_ascii=False))
        return 0
    for entry in result["records"]:
        print(text("sizes_row", config.language, issue=entry["issue"], pr=entry["pr"],
                   expected=_counts(entry.get("expected_size"), config.language),
                   actual=_counts(entry.get("actual_size"), config.language)))
    share = "-" if result["wrong_share"] is None else f"{result['wrong_share']:.0%}"
    print(text("sizes_summary", config.language, share=share, **{k: result[k] for k in ("wrong", "compared", "unknown")}))
    return 0


def _counts(size: dict | None, language: str) -> str:
    if not isinstance(size, dict):
        return text("sizes_unknown", language)
    return "/".join(str(size.get(name, "?")) for name in COUNTS)
