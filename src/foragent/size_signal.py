"""Expected change size beside the preread design, and its comparison after merge (#1696).

The worker reports ``SIZE: source=<n> tests=<n> files=<n>``. The estimate is recorded with
the preread answer but never enters the evidence key, so earlier clean prereads stay usable.
A split suggestion is derived from the estimate and the current ``size_limits`` each time it
is shown; it informs the person and never refuses publication or approval. After a merge,
one row with the expected and the measured size is appended to ``sizes.jsonl``.
"""

from datetime import UTC, datetime
import hashlib
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
FROZEN = "run-estimates"


def body_key(body: str) -> str:
    """The authored content of an issue body: the same key whatever the gate or publish wrote on it.

    Plan-child, owner, decision, ``foragent:write`` and ``foragent:waiting-prior`` marker lines, the
    Waiting section and the Done when removal record are gate writing (``issue.authored_body``).
    """
    from foragent import order_done_when
    from foragent.issue import authored_body

    return hashlib.sha256(authored_body(order_done_when.without_record(body)).encode("utf-8")).hexdigest()


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


def retain(plan: dict, root: Path, config, store, by_number: dict, mapping: dict, tracker) -> None:
    """Once every body of a publication is written, record each published body with its estimate.

    Called before any approval label or gate wake-up, so no run of these bodies can start before
    its input is recorded. Every published legacy issue and edit gets an entry, so the latest
    entry for a body always describes its latest publication: the estimate of the preread whose
    evidence key is this exact published plan, or unknown (null) when no such preread exists.
    A publication that stopped before writing records nothing; a failure never fails publication.
    """
    from foragent import order

    try:
        try:
            records = order._preread_records(root)
        except order.PlanError:
            records = {}
        items = [item for item in plan["issues"] if "statements" not in item and item["key"] in mapping]
        items += [order.edit_view(item, by_number[item["number"]], config) for item in plan.get("edits", [])
                  if "statements" not in item and item["number"] in by_number]
        waiting, _ = order._uncreated_lanes(plan, config, items)
        for item in items:
            number = mapping[item["key"]] if not item.get("edit") else int(item["key"].lstrip("#"))
            record = None
            if item["key"] not in waiting:  # no implementer read it before its lane existed
                record = records.get(order.preread_target(plan, item, config, root)[2])
            record = record if isinstance(record, dict) else {}
            size = record.get("size")
            _append(store.root, ESTIMATES, {
                "issue": number, "body": body_key(tracker.get_issue(number).body),
                "files": record.get("files") or {}, "size": size if isinstance(size, dict) else None,
                "at": datetime.now(UTC).isoformat(timespec="seconds")})
    except (OSError, ValueError, KeyError, order.PlanError):
        pass


def freeze(store, number: int, approval, restarted: bool) -> None:
    """At run start, fix this run's expected size; a restart of the same run keeps it.

    The value belongs to the run, not to whatever approval is current later: a reapproval during
    the run, a republication or a reread never changes it. It is the latest publication entry
    for the approved authored body; if that entry's files disagree with the approval snapshot,
    or there is no entry or no approval, it is unknown. Older entries are never fallen back on.
    A failure never stops the run start.
    """
    path = store.root / FROZEN / f"{number}.json"
    try:
        if restarted and path.exists():
            return
        size = None
        if approval is not None:
            key, snapshot, latest = body_key(approval.body), approval.acceptance_blobs or {}, None
            for entry in _entries(store.root, ESTIMATES):
                if entry.get("issue") == number and entry.get("body") == key:
                    latest = entry
            files = (latest or {}).get("files")
            if isinstance(files, dict) and all(snapshot.get(name, sha) == sha for name, sha in files.items()):
                size = latest.get("size") if isinstance(latest.get("size"), dict) else None
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        temporary.write_text(json.dumps({
            "issue": number, "approval_body_sha": getattr(approval, "body_sha", None),
            "approved_at": getattr(approval, "approved_at", None), "expected_size": size}), encoding="utf-8")
        os.rename(temporary, path)  # atomic on POSIX, like the store's own record writes
    except (OSError, ValueError):
        return


def frozen(store, number: int) -> dict | None:
    """The expected size fixed at the start of this issue's current run, or None (unknown)."""
    try:
        value = json.loads((store.root / FROZEN / f"{number}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    size = value.get("expected_size") if isinstance(value, dict) else None
    return size if isinstance(size, dict) else None


def record(n: int, ctx, git, approval, base_sha: str, head_sha: str, pr: int, log=None) -> None:
    """Retain one merged comparison; a failure is logged and never stops the merge.

    The expected size is only the one fixed at run start (``freeze``); nothing is looked up again.
    """
    try:
        base = git._call(["merge-base", base_sha, head_sha]).strip()
        _append(ctx.store.root, JOURNAL, {
            "issue": n, "pr": pr, "expected_size": frozen(ctx.store, n),
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
