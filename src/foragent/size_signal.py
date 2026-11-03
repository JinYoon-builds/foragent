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
PUBLISHED = "size-publications.jsonl"
FROZEN = "run-estimates"


def body_key(body: str) -> str:
    """The authored content of an issue body: the same key whatever the gate or publish wrote on it.

    Plan-child, owner, decision, ``foragent:write`` and ``foragent:waiting-prior`` marker lines, the
    Waiting section and the Done when removal record are gate writing (``issue.authored_body``).
    """
    from foragent import order_done_when
    from foragent.issue import authored_body

    return hashlib.sha256(authored_body(order_done_when.without_record(body)).encode("utf-8")).hexdigest()


CORRUPT = {"corrupt": True}  # a line that did not parse: an interrupted or damaged write


def _append(root: Path, journal: str, entry: dict) -> None:
    """One O_APPEND line written in full or not at all reported as written.

    A short write is continued until every byte is out; any failure raises, so no caller
    believes a line exists that does not. The leading newline isolates an interrupted tail.
    """
    payload = ("\n" + json.dumps(entry, ensure_ascii=False) + "\n").encode("utf-8")
    root.mkdir(parents=True, exist_ok=True)
    fd = os.open(root / journal, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    try:
        view = memoryview(payload)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("short write")
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)


def _entries(root: Path, journal: str) -> list[dict]:
    """Every line in order; a line that does not parse stays in place as ``CORRUPT``.

    Readers that depend on the latest line treat a damaged line after it as unknown rather
    than letting the line before it answer.
    """
    try:
        content = (root / journal).read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return []
    entries = []
    for line in content.splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            entry = None
        entries.append(entry if isinstance(entry, dict) else CORRUPT)
    return entries


class RecordError(Exception):
    """An estimate input could not be recorded; the caller stops before anything depends on it."""


def note(store, record: dict, digest: str | None, language: str = "en") -> None:
    """Keep a preread's estimate under its evidence key, before the preread record itself.

    The note carries the preread record's own id. A run start reads it only through a publication
    fact naming that exact record (evidence key and id), so a note whose record was never written,
    or was replaced by an overlapping reread, is never read. If this write fails the preread fails
    and records nothing.
    """
    size = record.get("size")
    try:
        _append(store.root, ESTIMATES, {
            "digest": digest, "record": record.get("id"), "key": record.get("key"),
            "files": record.get("files") or {}, "size": size if isinstance(size, dict) else None,
            "at": record.get("at")})
    except (OSError, ValueError) as exc:
        raise RecordError(text("size_record_failed", language, error=exc)) from exc


def _run_path(store, number: int, run) -> Path:
    """The record of exactly this run: its issue and the time it started."""
    identity = hashlib.sha256(str(run.started_at).encode("utf-8")).hexdigest()[:32]
    return store.root / FROZEN / f"{number}-{identity}.json"


def _published_items(plan: dict, config, by_number: dict, mapping: dict) -> list[tuple[int, dict]]:
    """(issue number, preread item) for each published legacy child already numbered and each edit."""
    from foragent import order

    items = [(mapping[item["key"]], item) for item in plan["issues"] if "statements" not in item and item["key"] in mapping]
    items += [(item["number"], order.edit_view(item, by_number[item["number"]], config)) for item in plan.get("edits", [])
              if "statements" not in item and item["number"] in by_number]
    return items


def publishing(plan: dict, config, store, by_number: dict, mapping: dict, language: str = "en") -> None:
    """Before any issue write, mark each existing published issue as being republished.

    Until ``published`` records the outcome, a run start reads such an issue as unknown, so a
    publication that stops or fails to record its outcome never leaves an older fact standing
    in. If this mark cannot be written, the publication stops before writing anything.
    """
    try:
        for number, _ in _published_items(plan, config, by_number, mapping):
            _append(store.root, PUBLISHED, {"issue": number, "phase": "publishing",
                                            "at": datetime.now(UTC).isoformat(timespec="seconds")})
    except (OSError, ValueError) as exc:
        raise RecordError(text("size_record_failed", language, error=exc)) from exc


def published(plan: dict, root: Path, config, store, by_number: dict, mapping: dict, tracker) -> None:
    """Record the one fact a publication knows: which preread each published body was read with.

    Called once every body of the publication is written and before any approval label or gate
    wake-up. For each published legacy child and edit it appends the issue number, the authored
    key of the body as written, and the evidence key of the preread of this exact published plan,
    or None when no preread read it (a reference change without a reread, a lane not yet created).
    No estimate is copied here; the run start reads it from the preread note of that key. A failed
    write leaves the issue's last line at ``publishing`` (unknown), never an older fact.
    """
    from foragent import order

    try:
        try:
            records = order._preread_records(root)
        except order.PlanError:
            records = {}
        items = _published_items(plan, config, by_number, mapping)
        waiting, _ = order._uncreated_lanes(plan, config, [item for _, item in items])
        for number, item in items:
            digest = record = None
            if item["key"] not in waiting:
                key = order.preread_target(plan, item, config, root)[2]
                if isinstance(records.get(key), dict) and records[key].get("id"):
                    digest, record = key, records[key]["id"]  # the record publication saw, not a copy of it
            _append(store.root, PUBLISHED, {
                "issue": number, "phase": "published", "body": body_key(tracker.get_issue(number).body),
                "digest": digest, "record": record, "at": datetime.now(UTC).isoformat(timespec="seconds")})
    except (OSError, ValueError, KeyError, order.PlanError):
        pass


def _approved_estimate(store, number: int, approval) -> dict | None:
    """The estimate of the preread the approved body was published with, else unknown.

    The latest publication fact for this issue and the approved authored body names the preread
    record publication saw (evidence key and record id); the note of exactly that record gives the
    estimate, if the files it bound agree with the approval snapshot.
    No publication fact, a republication still marked ``publishing`` (its outcome never recorded),
    a publication no preread read, or a disagreeing snapshot reads unknown.
    """
    if approval is None:
        return None
    key, chosen = body_key(approval.body), None
    lines = _entries(store.root, PUBLISHED)
    mine = [index for index, fact in enumerate(lines) if fact.get("issue") == number]
    if not mine or lines[mine[-1]].get("phase") != "published" or CORRUPT in lines[mine[-1]:]:
        return None  # never published, an unrecorded republication outcome, or a damaged later line
    for index in mine:
        if lines[index].get("phase") == "published" and lines[index].get("body") == key:
            chosen = lines[index]
    if not chosen or not chosen.get("digest") or not chosen.get("record"):
        return None
    note = None
    for entry in _entries(store.root, ESTIMATES):
        if entry.get("digest") == chosen["digest"] and entry.get("record") == chosen["record"]:
            note = entry
    snapshot, files = approval.acceptance_blobs or {}, (note or {}).get("files")
    if not isinstance(files, dict) or any(snapshot.get(name, sha) != sha for name, sha in files.items()):
        return None
    size = note.get("size")
    return size if isinstance(size, dict) else None


def freeze(store, number: int, approval, run, previous, restarted: bool) -> None:
    """At run start, write this run's own record once; no other record is ever read for it.

    A fresh start fixes the estimate the approved prereads agreed on. A restart (stall, crash,
    limit wait) continues the same run, so it carries exactly the record of the run it resumes,
    or unknown when that run has none. Any failure leaves this run without a record: unknown.
    """
    try:
        if restarted:
            source = _run_path(store, number, previous) if previous is not None else None
            value = json.loads(source.read_text(encoding="utf-8")) if source and source.exists() else {}
            size = value.get("expected_size") if isinstance(value, dict) else None
        else:
            size = _approved_estimate(store, number, approval)
        path = _run_path(store, number, run)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        temporary.write_text(json.dumps({
            "issue": number, "run_started_at": run.started_at,
            "approval_body_sha": getattr(approval, "body_sha", None),
            "expected_size": size if isinstance(size, dict) else None}), encoding="utf-8")
        os.rename(temporary, path)  # atomic on POSIX, like the store's own record writes
    except (OSError, ValueError, AttributeError):
        return


def frozen(store, number: int) -> dict | None:
    """The expected size the current run of this issue recorded at its own start, or None (unknown)."""
    try:
        run = store.get_run(number)
        value = json.loads(_run_path(store, number, run).read_text(encoding="utf-8")) if run else None
    except (OSError, ValueError, AttributeError):
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
