"""Claude Code statusline wrapper: record rate limits, pass the user's statusline through.

Account-wide files under $XDG_DATA_HOME/foragent (default ~/.local/share/foragent):
  claude-rate-limits.json  {seen_at: ISO UTC, rate_limits: the input object unchanged}
  statusline-wrapper.json  {enabled: bool, previous_command: str | null}
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


def is_wrapper(command: str) -> bool:
    """True when a shell command runs `fa statusline` (any spacing, quoting or path to fa)."""
    try:
        words = shlex.split(command)
    except ValueError:
        return False
    while words and "=" in words[0] and not words[0].startswith("="):
        words = words[1:]  # leading VAR=value assignments
    if not words:
        return False
    program = Path(words[0]).name
    if program == "fa":
        return words[1:] == ["statusline"]
    return program.startswith("python") and words[1:] == ["-m", "foragent.cli", "statusline"]


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


def run(stdin: BinaryIO | None = None, stdout: BinaryIO | None = None) -> int:
    """Entry point for `fa statusline`; stdout is the previous command's bytes only."""
    stdin = stdin or sys.stdin.buffer
    stdout = stdout or sys.stdout.buffer
    raw = stdin.read()
    try:
        record_usage(raw)
    except OSError as exc:
        # Recording is a side channel; the user's statusline must keep working.
        print(f"fa statusline: could not record rate limits: {exc}", file=sys.stderr)
    record = load_record() or {}
    previous = record.get("previous_command")
    if (not isinstance(previous, str) or not previous.strip() or is_wrapper(previous)
            or os.environ.get(NESTED_ENV)):
        return 0
    env = {**os.environ, NESTED_ENV: "1"}
    result = subprocess.run(previous, shell=True, input=raw, stdout=subprocess.PIPE, env=env, check=False)
    stdout.write(result.stdout)
    stdout.flush()
    return result.returncode if result.returncode >= 0 else 128 - result.returncode


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
        return "kept"
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
        try:
            _write_json(settings_file.resolve(), settings)
        except OSError:
            # Not installed: forget the answer so a later init can finish the install.
            record_path().unlink(missing_ok=True)
            raise
    return "installed"


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


def uninstall(language: str = "en") -> tuple[int, str]:
    """Undo the wrapper: put back the recorded command, or remove our statusLine.

    Only the statusLine value (or member) changes; every other byte of
    settings.json stays. Returns (exit code, message for the user).
    """
    from foragent.i18n import text

    settings_file = claude_settings_path()
    with _install_lock():
        record = load_record()
        if not record or not record.get("enabled"):
            return 0, text("statusline_not_installed", language)
        try:
            raw = settings_file.read_bytes()
        except FileNotFoundError:
            return 1, text("statusline_settings_missing", language, path=settings_file)
        except OSError as exc:
            return 1, text("statusline_settings_unreadable", language, path=settings_file,
                           error=exc.strerror or exc)
        try:
            content = raw.decode("utf-8")
            settings = json.loads(content)
            if not isinstance(settings, dict):
                raise ValueError("settings is not a JSON object")
            span = _member_span(content, "statusLine")
        except ValueError as exc:
            return 1, text("statusline_settings_unreadable", language, path=settings_file, error=exc)
        current = settings.get("statusLine")
        command = current.get("command") if isinstance(current, dict) else None
        if (span is None or not isinstance(command, str) or current.get("type") != "command"
                or not is_wrapper(command)):
            return 0, text("statusline_user_changed", language, path=settings_file)
        removal_start, value_start, value_end, removal_end = span
        previous = record.get("previous_command")
        intended = dict(settings)
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
        if json.loads(updated) != intended:
            # Never write a file whose effective settings differ from what we report.
            return 1, text("statusline_settings_unreadable", language, path=settings_file,
                           error="could not edit statusLine without changing other settings")
        _write_bytes(settings_file.resolve(), updated.encode("utf-8"))
        _write_json(record_path(), {**record, "enabled": False})
    return 0, message
