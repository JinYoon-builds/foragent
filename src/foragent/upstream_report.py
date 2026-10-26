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
from tempfile import NamedTemporaryFile
from typing import Any

import foragent
from foragent.state import StateStore

UPSTREAM = "JinYoon-builds/foragent"
_FRAME = re.compile(r'File "([^"]+)", line (\d+), in ([\w<>.]+)')
_TYPE = r"(?:[A-Za-z_]\w*\.)*[A-Z]\w*"
_TYPE_LINE = re.compile(rf"^({_TYPE})(?::|$)")
_TYPE_INLINE = re.compile(rf"(?<![\w.])({_TYPE}):(?=\s|$)")
# Run fields that are counts or durations; everything else in a run can hold private text.
_RUN_NUMBERS = ("attempts", "restarts", "transient_errors", "resume_failures", "rebuild_count",
                "waited_seconds", "executor_waited_seconds", "review_pass_to_merge_seconds",
                "merge_lock_held_seconds")


class ReportError(ValueError):
    """A report could not be prepared or sent; the message is for a person."""


def package_frames(text: str) -> list[tuple[str, int, str]]:
    """Frames whose file is part of this foragent package, as (foragent/<path>, line, function)."""
    package = Path(foragent.__file__).resolve().parent
    found = []
    for path, line, function in _FRAME.findall(text):
        parts = Path(path).parts
        if "foragent" not in parts:
            continue
        index = len(parts) - 1 - parts[::-1].index("foragent")
        inside = parts[index + 1:]
        if inside and inside[-1].endswith(".py") and package.joinpath(*inside).is_file():
            found.append(("/".join(("foragent", *inside)), int(line), function))
    return found


def error_type(text: str) -> str | None:
    """The exception class name (without its module) a traceback ends with."""
    matches = _FRAME.finditer(text)
    last = None
    for last in matches:
        pass
    tail = text[last.end():] if last else text
    name = None
    if "\n" in tail:
        for line in tail.splitlines():
            found = _TYPE_LINE.match(line)
            if found and not line[:1].isspace():
                name = found.group(1)
    if name is None:
        found = _TYPE_INLINE.search(tail)
        name = found.group(1) if found else None
    return name.rsplit(".", 1)[-1] if name else None


def signature(kind: str, frame: tuple[str, int, str] | None) -> str:
    """A stable search token from the error type and the innermost Foragent frame only."""
    where = f"{frame[0]}:{frame[2]}" if frame else "-"
    return "fa-sig-" + hashlib.sha256(f"{kind}|{where}".encode()).hexdigest()[:12]


def _source(store: StateStore, source: str) -> tuple[str, int | None, str]:
    """The local text for flag:N or event:N, the issue it belongs to, and its kind label."""
    kind, _, number = source.partition(":")
    if kind not in {"flag", "event"} or not number.isdigit() or int(number) < 1:
        raise ReportError("report_bad_source")
    if kind == "flag":
        flag = store.load_flag(int(number))
        if flag is None:
            raise ReportError("report_source_missing")
        return flag.reason, flag.number, "flag"
    try:
        lines = (store.root / "orch-events.jsonl").read_text(encoding="utf-8").splitlines()
        event = json.loads(lines[int(number) - 1])
    except (FileNotFoundError, IndexError, ValueError):
        raise ReportError("report_source_missing") from None
    issue = event.get("issue")
    return str(event.get("summary", "")), issue if type(issue) is int else None, "event"


def _number(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:g}"


def propose(store: StateStore, source: str, config_keys: list[str]) -> dict[str, Any]:
    """Prepare and save a report from one local record; nothing leaves the machine."""
    text, issue, kind = _source(store, source)
    name = error_type(text)
    if name is None:
        raise ReportError("report_no_error")
    frames = package_frames(text)
    innermost = frames[-1] if frames else None
    token = signature(name, innermost)
    where = f"{innermost[0]} ({innermost[2]})" if innermost else "outside the foragent package"
    title = f"{name} in {where}"
    lines = [f"Foragent {foragent.__version__} hit {name} in {where}.", "",
             f"Signature: {token}", "", f"Error type: {name}", f"Source: {kind}", "",
             "Foragent frames (innermost last):"]
    lines += [f"- {path}:{line} in {function}" for path, line, function in frames] or ["- none"]
    run = store.get_run(issue) if issue is not None else None
    if run is not None:
        known = {item.name for item in fields(run)}
        values = [(key, getattr(run, key)) for key in _RUN_NUMBERS if key in known]
        lines += ["", "Counts and durations:"]
        lines += [f"- {key}: {_number(value)}" for key, value in values
                  if isinstance(value, (int, float)) and not isinstance(value, bool)]
    lines += ["", "Config keys set: " + (", ".join(config_keys) or "none"), "",
              "Prepared by `fa report propose` and sent after review. It holds no code, issue text, "
              "file names outside Foragent, repository names or config values."]
    body = "\n".join(lines) + "\n"
    report_id = "rep-" + hashlib.sha256(f"{title}\n{body}".encode()).hexdigest()[:10]
    report = {"id": report_id, "title": title, "body": body, "signature": token}
    _save(store, {**report, "sent": None})
    return report


def _path(store: StateStore, report_id: str) -> Path:
    if not re.fullmatch(r"rep-[0-9a-f]{10}", report_id):
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
    try:
        return json.loads(_path(store, report_id).read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        raise ReportError("report_unknown") from None


def send(store: StateStore, report: dict[str, Any], tracker) -> tuple[str, str]:
    """Search upstream by signature, then append to the matching open issue or open one.

    Returns ("comment" | "create", issue URL).
    """
    rows = tracker.search_issues(f'repo:{UPSTREAM} is:issue is:open "{report["signature"]}"')
    match = next((row for row in rows if str(row.get("state", "")).lower() == "open"
                  and report["signature"] in f"{row.get('title') or ''}\n{row.get('body') or ''}"), None)
    if match is not None:
        number = int(match["number"])
        tracker.comment(number, report["body"])
        action = "comment"
    else:
        number = tracker.create_issue(report["title"], report["body"], [])
        action = "create"
    url = f"https://github.com/{UPSTREAM}/issues/{number}"
    _save(store, {**report, "sent": {"action": action, "number": number, "url": url}})
    return action, url
