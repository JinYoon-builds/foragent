"""Upstream error reports: prepared locally, shown in full, sent only on approval (#419).

A report holds only Foragent's version, config key names, counts and durations,
the error type and the stack frames inside the foragent package. Everything else
in the local record (messages, user code, issue text, other paths, repository
names, config values) stays on this machine.
"""

import ast
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
_FRAME = re.compile(r'File "([^"]+)", line (\d+), in ([\w<>.]+)')
_TYPE = r"(?:[A-Za-z_]\w*\.)*[A-Z]\w*"
_TYPE_INLINE = re.compile(rf"(?<![\w.])({_TYPE})(?::(?=\s|$)|$)")
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


def _verified_frame(path: str, line: str, function: str) -> tuple[str, int, str] | None:
    """A frame whose file is in this package and defines ``function``, as (foragent/<path>, line, function).

    Traceback text is free text, so look-alike frames from user code or messages are dropped.
    The line number is kept as a plain number; it may come from another installed version.
    """
    file = _package_file(path)
    if file is None:
        return None
    try:
        source = file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    if function not in _SYNTHETIC_FUNCTIONS and not re.search(
            rf"^\s*(?:async\s+def|def|class)\s+{re.escape(function)}\b", source, re.M):
        return None
    package = Path(foragent.__file__).resolve().parent
    return "/".join(("foragent", *file.relative_to(package).parts)), int(line), function


def package_frames(text: str) -> list[tuple[str, int, str]]:
    """Verified frames of this foragent package, as (foragent/<path>, line, function)."""
    return [frame for frame in (_verified_frame(*found) for found in _FRAME.findall(text)) if frame]


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


def _code_line(prefix: str) -> bool:
    """Whether ``prefix`` is one complete Python statement, as a traceback shows a frame's code."""
    code = re.sub(r"[\s~^]+$", "", prefix)
    for candidate in (code, code + " pass"):
        try:
            ast.parse(candidate)
            return True
        except (SyntaxError, ValueError):
            continue
    return False


def error_type(text: str) -> str | None:
    """The exception class a traceback ends with, without its module; None unless it is a real class.

    Flags keep line breaks and journal events join them, so both are read joined, by one rule:
    after the last frame, the exception line starts where everything before it is the frame's
    complete code line (or nothing). Text inside that code (``raise ValueError("KeyError: x")``)
    and message text after the exception name are never taken as the type.
    """
    joined = " ".join(text.split())
    last = None
    for last in _FRAME.finditer(joined):
        pass
    tail = joined[last.end():] if last else joined
    for match in _TYPE_INLINE.finditer(tail):
        prefix = tail[:match.start()].strip()
        if (not prefix or (last and _code_line(prefix))) and _known_exception(match.group(1)):
            return match.group(1).rsplit(".", 1)[-1]
    return None


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


def propose(store: StateStore, source: str, config_keys: list[str], language: str = "en") -> dict[str, Any]:
    """Prepare and save a report from one local record; nothing leaves the machine.

    The prose follows ``language``; the shown text is exactly what `fa report send` sends.
    """
    from foragent.i18n import text as say

    record, issue, kind = _source(store, source)
    name = error_type(record)
    if name is None:
        raise ReportError("report_no_error")
    frames = package_frames(record)
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
    lines += ([say("report_frame", language, path=path, line=line, function=function)
               for path, line, function in frames] or ["- " + say("report_none", language)])
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
