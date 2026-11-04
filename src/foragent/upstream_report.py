"""Upstream error reports: prepared locally, shown in full, sent only on approval (#419).

A report holds only Foragent's version, config key names, counts and durations,
the error type and the stack frames inside the foragent package. Everything else
in the local record (messages, user code, issue text, other paths, repository
names, config values) stays on this machine.
"""

from dataclasses import fields
import hashlib
import json
from pathlib import Path
import re
import subprocess
from tempfile import NamedTemporaryFile
from typing import Any

import foragent
from foragent.orch_tick_outcome import LOG_FILE as TICK_FAILURE_LOG
from foragent.state import StateStore
from foragent.tracker import TrackerError
from foragent.upstream_traceback import _joined_traceback, _log_traceback, parse_traceback  # noqa: F401

UPSTREAM = "JinYoon-builds/foragent"
_LOG_TAIL = 2_000_000  # Bytes read from the end of a log; tracebacks are appended last.
# Run fields that are counts or durations; everything else in a run can hold private text.
_RUN_NUMBERS = ("attempts", "restarts", "transient_errors", "resume_failures", "rebuild_count",
                "waited_seconds", "executor_waited_seconds", "review_pass_to_merge_seconds",
                "merge_lock_held_seconds")


class ReportError(ValueError):
    """A report could not be prepared or sent: a message key plus values for its text.

    Every failure of `fa report` is turned into one of these, so the person reads it in the
    repository's language; ``error`` carries the underlying tool's own words for diagnosis.
    """

    def __init__(self, key: str, **values: Any):
        super().__init__(key)
        self.values = values


def _detail(exc: BaseException) -> str:
    return " ".join(str(exc).split())[:300] or type(exc).__name__


# What a GitHub call can raise: gh failures, bad JSON, missing gh, timeouts.
_UPSTREAM_ERRORS = (TrackerError, OSError, ValueError, subprocess.SubprocessError)


def signature(kind: str, frame: tuple[str, int, str] | None) -> str:
    """A stable search token from the error type and the innermost Foragent frame only."""
    where = f"{frame[0]}:{frame[2]}" if frame else "-"
    return "fa-sig-" + hashlib.sha256(f"{kind}|{where}".encode()).hexdigest()[:12]


def _read_log(path: Path) -> str:
    """The tail of a local log (tracebacks are appended at the end); empty if it is missing."""
    try:
        with path.open("rb") as stream:
            stream.seek(max(0, path.stat().st_size - _LOG_TAIL))
            return stream.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def _issue_records(store: StateStore, issue: int) -> list[tuple[str, str]]:
    """Where an issue's failure is recorded, as (kind, text): the runner flags `retries-exceeded`
    and appends the traceback to the issue log, so the flag reason alone rarely holds it."""
    flag = store.load_local_flag(issue)
    run = store.get_run(issue)
    return [("text", flag.reason if flag else ""), ("text", run.detail if run else ""),
            ("log", _read_log(store.log_path(issue)))]


def _source(store: StateStore, source: str) -> tuple[list[tuple[str, str]], int | None, str]:
    """The local records for flag:N or event:N in the order they are read, the issue, and the kind.

    Each record is ("text", text), ("joined", one-line journal text) or ("log", log tail).
    """
    kind, _, number = source.partition(":")
    if kind not in {"flag", "event"} or not number.isdigit() or int(number) < 1:
        raise ReportError("report_bad_source")
    if kind == "flag":
        flag = store.load_local_flag(int(number))
        if flag is None:
            raise ReportError("report_source_missing")
        return _issue_records(store, flag.number), flag.number, "flag"
    try:
        lines = (store.root / "orch-events.jsonl").read_text(encoding="utf-8").splitlines()
        event = json.loads(lines[int(number) - 1])
    except (FileNotFoundError, IndexError, ValueError):
        raise ReportError("report_source_missing") from None
    issue = event.get("issue")
    issue = issue if type(issue) is int else None
    summary = str(event.get("summary", ""))
    records = _issue_records(store, issue) if issue is not None else []
    # The journal joins lines; the issue's flag or run detail may keep this same traceback whole.
    same = [record for record in records[:2] if "\n" in record[1] and " ".join(record[1].split())
            and (" ".join(record[1].split()) in summary or summary in " ".join(record[1].split()))]
    tick_log = store.root / TICK_FAILURE_LOG
    # A failed gate tick writes its traceback to this log and names the log in its request event.
    tick = [("log", _read_log(tick_log))] if issue is None and str(tick_log) in summary else []
    return [*same, ("joined", summary), *records, *tick], issue, "event"


def _read_record(kind: str, text: str) -> tuple[str | None, list[tuple[str, int, str]]]:
    if kind == "log":
        return _log_traceback(text)
    if kind == "joined" or "\n" not in text:
        return _joined_traceback(text)
    return parse_traceback(text)


def _number(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:g}"


def propose(store: StateStore, source: str, config_keys: list[str], language: str = "en") -> dict[str, Any]:
    """Prepare and save a report from one local record; nothing leaves the machine.

    The prose follows ``language``; the shown text is exactly what `fa report send` sends.
    """
    from foragent.i18n import text as say

    records, issue, kind = _source(store, source)
    # A record with Foragent frames wins: a flag reason such as "Runner failed: KeyError: 'pid'"
    # names the error but not where it happened, which the issue log's traceback shows.
    found = [result for result in map(lambda record: _read_record(*record), records) if result[0]]
    name, frames = next((result for result in found if result[1]), found[0] if found else (None, []))
    if name is None:
        raise ReportError("report_no_error")
    innermost = frames[-1] if frames else None
    token = signature(name, innermost)
    where = (say("report_where_frame", language, path=innermost[0], function=innermost[2]) if innermost
             else say("report_where_outside", language))
    title = say("report_title", language, error=name, where=where)
    lines = [say("report_intro", language, version=foragent.__version__, error=name, where=where), "",
             say("report_signature", language, signature=token), "",
             say("report_error_type", language, error=name),
             say("report_source", language, source=kind), "",
             say("report_frames", language)]
    lines += ([say("report_frame" if line else "report_frame_no_line", language, path=path, line=line,
                   function=function) for path, line, function in frames] or ["- " + say("report_none", language)])
    run = store.get_run(issue) if issue is not None else None
    if run is not None:
        known = {item.name for item in fields(run)}
        values = [(key, getattr(run, key)) for key in _RUN_NUMBERS if key in known]
        lines += ["", say("report_counts", language)]
        lines += [f"- {key}: {_number(value)}" for key, value in values
                  if isinstance(value, (int, float)) and not isinstance(value, bool)]
    lines += ["", say("report_config_keys", language, keys=", ".join(config_keys) or say("report_none", language)),
              "", say("report_footer", language)]
    body = "\n".join(lines) + "\n"
    report = {"id": _report_id(title, body, token), "title": title, "body": body, "signature": token}
    try:
        _save(store, {**report, "sent": None})
    except OSError as exc:
        raise ReportError("report_save_failed", error=_detail(exc)) from exc
    return report


def _report_id(title: str, body: str, token: str) -> str:
    """The ID a person approves names the exact outbound text: a hash of title, body and signature."""
    return "rep-" + hashlib.sha256(json.dumps([title, body, token]).encode()).hexdigest()[:16]


def _path(store: StateStore, report_id: str) -> Path:
    if not re.fullmatch(r"rep-[0-9a-f]{16}", report_id):
        raise ReportError("report_unknown")
    return store.root / "upstream-reports" / f"{report_id}.json"


def _save(store: StateStore, report: dict[str, Any]) -> None:
    path = _path(store, report["id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def load(store: StateStore, report_id: str) -> dict[str, Any]:
    """The saved report, only if its title, body and signature still hash to ``report_id``.

    The file is local and editable; a changed report is refused, so `fa report send` can only
    transmit the text `fa report propose` showed under that ID.
    """
    try:
        report = json.loads(_path(store, report_id).read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        raise ReportError("report_unknown") from None
    fields_ok = isinstance(report, dict) and all(isinstance(report.get(key), str) for key in
                                                  ("id", "title", "body", "signature"))
    if not fields_ok or report["id"] != report_id or not re.fullmatch(r"fa-sig-[0-9a-f]{12}", report["signature"]) \
            or _report_id(report["title"], report["body"], report["signature"]) != report_id:
        raise ReportError("report_changed")
    return report


def _searchable_text(row: dict[str, Any]) -> str:
    """Title, body and every text-match fragment GitHub returned, which includes comment matches.

    A search hit alone is not trusted (search is fuzzy); the signature must appear in this text.
    """
    fragments = [str(match.get("fragment") or "") for match in row.get("text_matches") or []
                 if isinstance(match, dict)]
    return "\n".join([str(row.get("title") or ""), str(row.get("body") or ""), *fragments])


def send(store: StateStore, report: dict[str, Any], tracker) -> tuple[str, str]:
    """Search upstream by signature, then append to the matching open issue or open one.

    Returns ("comment" | "create", issue URL). Every failure is a ReportError with a message key.
    """
    try:
        result = tracker.search_issues(f'repo:{UPSTREAM} is:issue is:open "{report["signature"]}"')
    except _UPSTREAM_ERRORS as exc:
        raise ReportError("report_upstream_failed", error=_detail(exc)) from exc
    rows = result.get("items") if isinstance(result, dict) else None
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise ReportError("report_search_invalid")
    match = next((row for row in rows if str(row.get("state", "")).lower() == "open"
                  and report["signature"] in _searchable_text(row)), None)
    # No match proves absence only if GitHub returned every result; otherwise a new issue may duplicate one.
    total = result.get("total_count")
    complete = (not result.get("incomplete_results") and isinstance(total, int) and total <= len(rows))
    if match is None and not complete:
        raise ReportError("report_search_incomplete")
    try:
        if match is not None:
            number = int(match["number"])
            tracker.comment(number, report["body"])
            action = "comment"
        else:
            number = tracker.create_issue(report["title"], report["body"], [])
            action = "create"
    except _UPSTREAM_ERRORS as exc:
        raise ReportError("report_upstream_failed", error=_detail(exc)) from exc
    url = f"https://github.com/{UPSTREAM}/issues/{number}"
    try:
        _save(store, {**report, "sent": {"action": action, "number": number, "url": url}})
    except OSError as exc:
        raise ReportError("report_sent_unrecorded", url=url, error=_detail(exc)) from exc
    return action, url
