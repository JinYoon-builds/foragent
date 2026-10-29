"""Claude Code statusline wrapper: record rate limits, pass the user's statusline through.

Account-wide files under $XDG_DATA_HOME/foragent (default ~/.local/share/foragent):
  claude-rate-limits.json  {seen_at: ISO UTC, rate_limits: the input object unchanged}
  statusline-wrapper.json  {enabled: bool, previous_command: str | null}

The install also adds Foragent's SessionStart hook (`fa statusline --session-start`)
beside the statusline in Claude's user settings; uninstalling or declining removes it.
"""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
import fcntl
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
from typing import Any, BinaryIO

WRAPPER_COMMAND = "fa statusline"
HOOK_COMMAND = "fa statusline --session-start"
# Sessions the person starts or resumes; not /clear or compaction inside a session.
HOOK_MATCHER = "startup|resume"
# Set while the previous command runs, so a nested wrapper never runs it again.
NESTED_ENV = "FORAGENT_STATUSLINE_ACTIVE"
PROMPT = ("Install the Foragent statusline in Claude Code? It records Claude's rate limits "
          "and still shows your current statusline unchanged. [y/N] ")


def data_dir() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local/share")
    return Path(base) / "foragent"


def usage_path() -> Path:
    return data_dir() / "claude-rate-limits.json"


def record_path() -> Path:
    return data_dir() / "statusline-wrapper.json"


def claude_settings_path() -> Path:
    base = os.environ.get("CLAUDE_CONFIG_DIR") or str(Path.home() / ".claude")
    return Path(base) / "settings.json"


def _write_json(path: Path, value: Any) -> None:
    """Replace the file atomically so a concurrent reader never sees half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _fa_arguments(command: str) -> list[str] | None:
    """The arguments a shell command gives fa (any spacing, quoting or path to fa), else None."""
    try:
        words = shlex.split(command)
    except ValueError:
        return None
    while words and "=" in words[0] and not words[0].startswith("="):
        words = words[1:]  # leading VAR=value assignments
    if not words:
        return None
    program = Path(words[0]).name
    if program == "fa":
        return words[1:]
    if program.startswith("python") and words[1:3] == ["-m", "foragent.cli"]:
        return words[3:]
    return None


def is_wrapper(command: str) -> bool:
    """True when a shell command runs `fa statusline`."""
    return _fa_arguments(command) == ["statusline"]


def is_session_hook(command: Any) -> bool:
    """True when a hook command runs `fa statusline --session-start`."""
    return isinstance(command, str) and _fa_arguments(command) == ["statusline", "--session-start"]


def has_hook(settings: dict) -> bool:
    hooks = settings.get("hooks")
    groups = hooks.get("SessionStart") if isinstance(hooks, dict) else None
    return isinstance(groups, list) and any(
        isinstance(group, dict) and isinstance(group.get("hooks"), list)
        and any(isinstance(hook, dict) and is_session_hook(hook.get("command")) for hook in group["hooks"])
        for group in groups)


def wrapper_active(settings: dict) -> bool:
    """True when the effective statusLine runs `fa statusline`."""
    current = settings.get("statusLine")
    return (isinstance(current, dict) and current.get("type") == "command"
            and isinstance(current.get("command"), str) and is_wrapper(current["command"]))


def with_hook(settings: dict) -> dict:
    """``settings`` with Foragent's SessionStart hook added (unchanged when it is there)."""
    if has_hook(settings):
        return settings
    hooks = settings.get("hooks")
    hooks = dict(hooks) if isinstance(hooks, dict) else {}
    groups = hooks.get("SessionStart")
    groups = list(groups) if isinstance(groups, list) else []
    groups.append({"matcher": HOOK_MATCHER, "hooks": [{"type": "command", "command": HOOK_COMMAND}]})
    return {**settings, "hooks": {**hooks, "SessionStart": groups}}


def without_hook(settings: dict) -> dict:
    """``settings`` without Foragent's SessionStart hook; emptied containers go too."""
    if not has_hook(settings):
        return settings
    hooks = dict(settings["hooks"])
    groups = []
    for group in hooks["SessionStart"]:
        if isinstance(group, dict) and isinstance(group.get("hooks"), list):
            kept = [hook for hook in group["hooks"]
                    if not (isinstance(hook, dict) and is_session_hook(hook.get("command")))]
            if len(kept) != len(group["hooks"]):
                if kept:
                    groups.append({**group, "hooks": kept})
                continue
        groups.append(group)
    if groups:
        hooks["SessionStart"] = groups
    else:
        del hooks["SessionStart"]
    result = dict(settings)
    if hooks:
        result["hooks"] = hooks
    else:
        del result["hooks"]
    return result


def load_record() -> dict | None:
    record = _read_json(record_path())
    return record if isinstance(record, dict) else None


def record_usage(raw: bytes, now: Callable[[], datetime] = lambda: datetime.now(UTC)) -> bool:
    """Save rate_limits from statusline input; leave the last observation when absent."""
    try:
        payload = json.loads(raw)
    except ValueError:
        return False
    limits = payload.get("rate_limits") if isinstance(payload, dict) else None
    if not isinstance(limits, dict):
        return False
    _write_json(usage_path(), {"seen_at": now().astimezone(UTC).isoformat(), "rate_limits": limits})
    return True


def _session_input(raw: bytes) -> dict:
    try:
        payload = json.loads(raw)
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}


def session_start(stdin: BinaryIO | None = None, stdout: BinaryIO | None = None) -> int:
    """Entry point for the SessionStart hook: one JSON object with the summary line, or nothing.

    Claude Code shows a hook's systemMessage to the person; plain stdout would reach only
    Claude. Outside a Foragent project, or with presence.session_start off, it prints nothing.
    """
    from foragent import presence

    stdin = stdin or sys.stdin.buffer
    stdout = stdout or sys.stdout.buffer
    payload = _session_input(stdin.read())
    cwd = payload.get("cwd")
    session_id = payload.get("session_id")
    try:
        message = presence.session_start_message(Path(cwd) if isinstance(cwd, str) and cwd else Path.cwd(),
                                                 session_id if isinstance(session_id, str) else None,
                                                 datetime.now(UTC))
    except Exception:  # A failed summary must never disturb the session that is starting.
        message = None
    if message:
        stdout.write((json.dumps({"systemMessage": message}, ensure_ascii=False) + "\n").encode("utf-8"))
        stdout.flush()
    return 0


def run(stdin: BinaryIO | None = None, stdout: BinaryIO | None = None) -> int:
    """Entry point for `fa statusline`: the previous command's bytes, plus presence in a project.

    During a session's first minute the big squirrel shows above that output.
    """
    stdin = stdin or sys.stdin.buffer
    stdout = stdout or sys.stdout.buffer
    raw = stdin.read()
    try:
        record_usage(raw)
    except OSError as exc:
        # Recording is a side channel; the user's statusline must keep working.
        print(f"fa statusline: could not record rate limits: {exc}", file=sys.stderr)
    if os.environ.get(NESTED_ENV):
        return 0  # The outer wrapper prints the user's output and presence once.
    record = load_record() or {}
    previous = record.get("previous_command")
    output, code = b"", 0
    if isinstance(previous, str) and previous.strip() and not is_wrapper(previous):
        env = {**os.environ, NESTED_ENV: "1"}
        result = subprocess.run(previous, shell=True, input=raw, stdout=subprocess.PIPE, env=env, check=False)
        output, code = result.stdout, (result.returncode if result.returncode >= 0 else 128 - result.returncode)
    from foragent import presence
    now = datetime.now(UTC)
    output = presence.append(output, presence.statusline_segment(Path.cwd(), now))
    session_id = _session_input(raw).get("session_id")
    art = presence.squirrel(Path.cwd(), session_id if isinstance(session_id, str) else None, now)
    stdout.write(art.encode("utf-8") + b"\n" + output if art else output)
    stdout.flush()
    return code


@contextmanager
def _install_lock() -> Iterator[None]:
    """Serialize concurrent `fa init` runs across processes for the decide-and-write step."""
    path = data_dir() / "statusline-wrapper.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _read_settings(settings_file: Path) -> dict | None:
    settings = _read_json(settings_file) if settings_file.exists() else {}
    return settings if isinstance(settings, dict) else None


def offer_install(ask: Callable[[str], str] = input) -> str:
    """Ask once whether to wrap Claude Code's statusline; remember either answer.

    Returns "installed", "declined", "kept" (already decided), "skipped" (no
    usable Claude settings) or "unanswered" (no answer could be read). Raises
    OSError when a file cannot be written; then nothing is remembered, so the
    next init asks again.
    """
    if load_record() is not None:
        return _sync_hook()
    settings_file = claude_settings_path()
    if not settings_file.parent.is_dir() or _read_settings(settings_file) is None:
        return "skipped"
    try:
        answer = ask(PROMPT)
    except (EOFError, OSError):
        return "unanswered"
    with _install_lock():
        # Another init may have decided while this one waited for the answer or the lock;
        # its record and settings are the truth, so re-read both here.
        if load_record() is not None:
            return "kept"
        if answer.strip().lower() not in {"y", "yes"}:
            _write_json(record_path(), {"enabled": False, "previous_command": None})
            _sync_hook_locked()
            return "declined"
        settings = _read_settings(settings_file)
        if settings is None:
            return "skipped"
        current = settings.get("statusLine")
        current = current if isinstance(current, dict) else {}
        command = current.get("command") if current.get("type") == "command" else None
        previous = command if isinstance(command, str) and command.strip() and not is_wrapper(command) else None
        # Record first: a wrapper in settings without its record would hide the user's statusline.
        _write_json(record_path(), {"enabled": True, "previous_command": previous})
        settings["statusLine"] = {**current, "type": "command", "command": WRAPPER_COMMAND}
        settings = with_hook(settings)
        try:
            _write_json(settings_file.resolve(), settings)
        except OSError:
            # Not installed: forget the answer so a later init can finish the install.
            record_path().unlink(missing_ok=True)
            raise
    return "installed"


def _sync_hook() -> str:
    with _install_lock():
        return _sync_hook_locked()


def _sync_hook_locked() -> str:
    """Match the SessionStart hook to the statusline: "hooked" when it was added, else "kept".

    Installs from before the hook existed get it at the next init. The hook is added
    only beside an active wrapper, so a statusLine the user replaced does not bring it
    back; a declined or uninstalled statusline has none.
    """
    record = load_record() or {}
    settings_file = claude_settings_path()
    settings = _read_settings(settings_file) if settings_file.parent.is_dir() else None
    if settings is None:
        return "kept"
    if not record.get("enabled"):
        wanted = without_hook(settings)
    elif wrapper_active(settings):
        wanted = with_hook(settings)
    else:
        return "kept"
    if wanted is settings:
        return "kept"
    _write_json(settings_file.resolve(), wanted)
    return "hooked" if record.get("enabled") else "kept"


_JSON_SPACE = " \t\r\n"


def _skip_space(text: str, at: int) -> int:
    while at < len(text) and text[at] in _JSON_SPACE:
        at += 1
    return at


def _member_span(text: str, name: str) -> tuple[int, int, int, int] | None:
    """Find the last top-level member `name` in a JSON object text.

    Returns (removal start, value start, value end, removal end): replacing
    [value start, value end) changes only the value; deleting [removal start,
    removal end) removes the member and exactly one separating comma.
    """
    decoder = json.JSONDecoder()
    at = _skip_space(text, 0)
    if text[at:at + 1] != "{":
        raise ValueError("settings is not a JSON object")
    members: list[tuple[int, int, int, int, str]] = []  # token start, value start, value end, separator end
    previous_end = at + 1
    at = _skip_space(text, at + 1)
    while text[at:at + 1] != "}":
        key, key_end = decoder.raw_decode(text, at)
        colon = _skip_space(text, key_end)
        if text[colon:colon + 1] != ":":
            raise ValueError("expected ':' in settings")
        value_start = _skip_space(text, colon + 1)
        _value, value_end = decoder.raw_decode(text, value_start)
        after = _skip_space(text, value_end)
        members.append((previous_end, value_start, value_end, after, key))
        if text[after:after + 1] == ",":
            previous_end = after + 1
            at = _skip_space(text, after + 1)
        elif text[after:after + 1] != "}":
            raise ValueError("expected ',' or '}' in settings")
        else:
            at = after
    for index in range(len(members) - 1, -1, -1):
        start, value_start, value_end, after, key = members[index]
        if key != name:
            continue
        if index + 1 < len(members):
            return start, value_start, value_end, after + 1  # also the following comma
        if index > 0:
            # Last member: drop the comma before it instead, keeping the closing layout.
            return members[index - 1][2], value_start, value_end, value_end
        return start, value_start, value_end, value_end
    return None


def _write_bytes(path: Path, data: bytes) -> None:
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(data)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def _strip_hook(content: str, intended: dict) -> tuple[str, dict]:
    """Remove Foragent's SessionStart hook from ``content``/``intended``, keeping other bytes."""
    stripped = without_hook(intended)
    if stripped is intended:
        return content, intended
    if "hooks" in stripped:
        # The last duplicate is the effective one; replace only its value.
        span = _member_span(content, "hooks")
        return (content[:span[1]] + json.dumps(stripped["hooks"], ensure_ascii=False)
                + content[span[2]:]), stripped
    while (span := _member_span(content, "hooks")) is not None:
        content = content[:span[0]] + content[span[3]:]
    return content, stripped


def uninstall(language: str = "en") -> tuple[int, str]:
    """Undo the wrapper: put back the recorded command, or remove our statusLine.

    Foragent's SessionStart hook goes too, whatever happened to the statusline:
    a statusLine the user changed, or a record that is missing or already off,
    still leaves no hook behind. Only those members change; every other byte of
    settings.json stays. Returns (exit code, message for the user).
    """
    from foragent.i18n import text

    settings_file = claude_settings_path()
    with _install_lock():
        record = load_record()
        installed = bool(record and record.get("enabled"))
        try:
            raw = settings_file.read_bytes()
        except FileNotFoundError:
            if not installed:
                return 0, text("statusline_not_installed", language)
            return 1, text("statusline_settings_missing", language, path=settings_file)
        except OSError as exc:
            if not installed:
                return 0, text("statusline_not_installed", language)
            return 1, text("statusline_settings_unreadable", language, path=settings_file,
                           error=exc.strerror or exc)
        try:
            content = raw.decode("utf-8")
            settings = json.loads(content)
            if not isinstance(settings, dict):
                raise ValueError("settings is not a JSON object")
            span = _member_span(content, "statusLine")
        except ValueError as exc:
            if not installed:
                return 0, text("statusline_not_installed", language)
            return 1, text("statusline_settings_unreadable", language, path=settings_file, error=exc)
        updated, intended = content, dict(settings)
        unwrap = installed and span is not None and wrapper_active(settings)
        if not installed:
            message = text("statusline_not_installed", language)
        elif not unwrap:
            message = text("statusline_user_changed", language, path=settings_file)
        else:
            current = settings["statusLine"]
            removal_start, value_start, value_end, removal_end = span
            previous = record.get("previous_command")
            if isinstance(previous, str) and previous.strip():
                intended["statusLine"] = {**current, "command": previous}
                value = json.dumps(intended["statusLine"], ensure_ascii=False)
                # The last duplicate is the one every JSON reader uses; earlier ones stay inactive.
                updated = content[:value_start] + value + content[value_end:]
                message = text("statusline_restored", language, command=previous.strip())
            else:
                del intended["statusLine"]
                updated = content[:removal_start] + content[removal_end:]
                # Duplicate members: removing the last would expose an earlier one, so remove them all.
                while (span := _member_span(updated, "statusLine")) is not None:
                    updated = updated[:span[0]] + updated[span[3]:]
                message = text("statusline_removed", language)
        unhooked = without_hook(intended) is not intended
        updated, intended = _strip_hook(updated, intended)
        if unhooked:
            message += "\n" + text("statusline_hook_removed", language)
        if updated == content:
            return 0, message
        if json.loads(updated) != intended:
            # Never write a file whose effective settings differ from what we report.
            return 1, text("statusline_settings_unreadable", language, path=settings_file,
                           error="could not edit settings without changing other settings")
        _write_bytes(settings_file.resolve(), updated.encode("utf-8"))
        if unwrap:
            _write_json(record_path(), {**record, "enabled": False})
    return 0, message
