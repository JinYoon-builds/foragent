"""Upstream error reports: prepared locally, shown in full, sent only on approval (#419).

A report holds only Foragent's version, config key names, counts and durations,
the error type and the stack frames inside the foragent package. Everything else
in the local record (messages, user code, issue text, other paths, repository
names, config values) stays on this machine.
"""

import builtins
from dataclasses import fields
import hashlib
import json
from pathlib import Path
import re
import sys
from tempfile import NamedTemporaryFile
from typing import Any

import foragent
from foragent.state import StateStore

UPSTREAM = "JinYoon-builds/foragent"
# The whole function token is captured (up to whitespace), so a frame is never verified by a prefix.
_FRAME = re.compile(r'File "([^"]+)", line (\d+), in (\S+)(?=\s|$)')
_TYPE = r"(?:[A-Za-z_]\w*\.)*[A-Z]\w*"
_TYPE_INLINE = re.compile(rf"(?<![\w.])({_TYPE})(?::(?=\s|$)|$)")
# Traceback structure: indented frame lines, then the exception line at column 0.
_FRAME_LINE = re.compile(r'^\s+File "([^"]+)", line (\d+), in (\S+)\s*$')
_TYPE_LINE = re.compile(rf"^({_TYPE})(?::(?=\s|$)|$)")
_HEADER = "Traceback (most recent call last):"
_SYNTHETIC_FUNCTIONS = {"<module>", "<lambda>", "<listcomp>", "<dictcomp>", "<setcomp>", "<genexpr>"}
# Run fields that are counts or durations; everything else in a run can hold private text.
_RUN_NUMBERS = ("attempts", "restarts", "transient_errors", "resume_failures", "rebuild_count",
                "waited_seconds", "executor_waited_seconds", "review_pass_to_merge_seconds",
                "merge_lock_held_seconds")


class ReportError(ValueError):
    """A report could not be prepared or sent; the message is for a person."""


def _package_file(path: str) -> Path | None:
    """The file ``path`` names if it really is a .py file of this foragent package, after resolving."""
    package = Path(foragent.__file__).resolve().parent
    try:
        resolved = Path(path).resolve()
        resolved.relative_to(package)
    except (OSError, RuntimeError, ValueError):
        return None
    return resolved if resolved.suffix == ".py" and resolved.is_file() else None


def _verified_frame(path: str, line: str, function: str) -> tuple[str, int | None, str] | None:
    """A frame rebuilt from this package's own files, as (foragent/<path>, line or None, function).

    Traceback text is free text, so every field reported is checked against the real file:
    the path is the resolved package file's own relative path; the function must be one plain
    name (Python prints ``co_name``, never a dotted name) that the file defines; the line number
    is kept only if the file has that line, since it may come from another installed version.
    Anything else drops the frame, so no unchecked text reaches a report.
    """
    file = _package_file(path)
    if file is None:
        return None
    try:
        source = file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    if function not in _SYNTHETIC_FUNCTIONS and not (
            re.fullmatch(r"[A-Za-z_]\w*", function) and re.search(
                rf"^\s*(?:async\s+def|def|class)\s+{re.escape(function)}\b", source, re.M)):
        return None
    number = int(line)
    lines = source.count("\n") + (0 if source.endswith("\n") else 1)
    package = Path(foragent.__file__).resolve().parent
    return ("/".join(("foragent", *file.relative_to(package).parts)),
            number if 1 <= number <= lines else None, function)


def _known_exception(name: str) -> bool:
    """Whether ``name`` is a real exception class: built-in, a loaded module's, or foragent's own."""
    module, _, cls = name.rpartition(".")
    candidates = [sys.modules.get(module)] if module else [builtins]
    if any(isinstance(getattr(owner, cls, None), type) and issubclass(getattr(owner, cls), BaseException)
           for owner in candidates if owner is not None):
        return True
    if module and not (module == "foragent" or module.startswith("foragent.")):
        return False
    package = Path(foragent.__file__).resolve().parent
    pattern = re.compile(rf"^class\s+{re.escape(cls)}\b", re.M)
    return any(pattern.search(path.read_text(encoding="utf-8")) for path in package.rglob("*.py"))


def parse_traceback(text: str) -> tuple[str | None, list[tuple[str, int, str]]]:
    """The final exception's class name and its verified Foragent frames, read from structure only.

    Python prints frames as indented ``File "...", line N, in f`` lines, then the exception as
    the first line at column 0. Source and caret lines are indented display text and are never
    read. With chained exceptions only the last ``Traceback`` block counts. The class name must
    be a real exception class, so free text at column 0 never becomes the type.
    """
    lines = text.splitlines()
    starts = [index for index, line in enumerate(lines) if line.rstrip().endswith(_HEADER)]
    block = lines[starts[-1] + 1:] if starts else lines
    raw, last = [], -1
    for index, line in enumerate(block):
        found = _FRAME_LINE.match(line)
        if found:
            raw.append(found.groups())
            last = index
    name = None
    line = next((item for item in block[last + 1:] if item.strip() and not item[:1].isspace()), "")
    found = _TYPE_LINE.match(line)
    if found and _known_exception(found.group(1)):
        name = found.group(1).rsplit(".", 1)[-1]
    return name, [frame for frame in (_verified_frame(*item) for item in raw) if frame]


def _joined_traceback(text: str) -> tuple[str | None, list[tuple[str, int, str]]]:
    """A journal line with its line breaks removed: frames stay readable, the type only if unambiguous."""
    last = None
    for last in _FRAME.finditer(text):
        pass
    tail = text[last.end():] if last else text
    names = {match.group(1).rsplit(".", 1)[-1] for match in _TYPE_INLINE.finditer(tail)
             if _known_exception(match.group(1))}
    frames = [frame for frame in (_verified_frame(*item) for item in _FRAME.findall(text)) if frame]
    return (names.pop() if len(names) == 1 else None), frames


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
    issue = issue if type(issue) is int else None
    summary = str(event.get("summary", ""))
    # The journal joins lines; the issue's own flag or run detail may keep the same traceback whole.
    originals = []
    if issue is not None:
        flag = store.load_flag(issue)
        run = store.get_run(issue)
        originals = [flag.reason if flag else "", run.detail if run else ""]
    for original in originals:
        joined = " ".join(original.split())
        if "\n" in original and joined and (joined in summary or summary in joined):
            return original, issue, "event"
    return summary, issue, "event"


def _number(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:g}"


def propose(store: StateStore, source: str, config_keys: list[str], language: str = "en") -> dict[str, Any]:
    """Prepare and save a report from one local record; nothing leaves the machine.

    The prose follows ``language``; the shown text is exactly what `fa report send` sends.
    """
    from foragent.i18n import text as say

    record, issue, kind = _source(store, source)
    name, frames = parse_traceback(record) if "\n" in record else _joined_traceback(record)
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
