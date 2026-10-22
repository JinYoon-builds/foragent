"""Open interactive design sessions outside the gate's lane/run lifecycle."""

import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
from uuid import uuid4

from foragent.state import StateStore


def _skill_source() -> str:
    # The repository is the canonical source; wheels carry the same file as data.
    relative = Path("skills/fa-design-session/SKILL.md")
    source = Path(__file__).resolve().parents[2] / relative
    if not source.is_file():
        source = Path(sys.prefix) / "share/foragent" / relative
    return source.read_text(encoding="utf-8")


def _request(argv: list[str], root: Path) -> str:
    try:
        return subprocess.run(argv, cwd=root, check=True, capture_output=True,
                              text=True, timeout=10).stdout.strip()
    except subprocess.CalledProcessError as exc:
        detail = " ".join((exc.stderr or "").split())[:200]
        raise OSError(f"{Path(argv[0]).name} {argv[1]} failed ({exc.returncode}): {detail}") from exc
    except subprocess.TimeoutExpired as exc:
        raise OSError(f"{Path(argv[0]).name} {argv[1]} timed out") from exc


def _orca_handle(output: str) -> str:
    record = json.loads(output)
    # Orca's JSON envelope wraps the terminal in result; older CLIs return it directly.
    if isinstance(record, dict) and isinstance(record.get("result"), dict):
        record = record["result"]
    if isinstance(record, dict):
        handle = record.get("handle") or record.get("terminal")
        if isinstance(handle, dict):
            handle = handle.get("handle")
        if isinstance(handle, str) and handle:
            return handle
    raise OSError("Orca did not return a design terminal handle")


def open_session(root: Path, store: StateStore, *, topic: str | None = None,
                 language: str = "en") -> Path:
    """Launch Claude in a new tab/window and return without watching its lifetime."""
    root = root.resolve()
    claude = shutil.which("claude")
    if not claude:
        raise OSError("fa design open requires Claude Code (claude)")
    orca = None
    if os.environ.get("ORCA_TERMINAL_HANDLE"):
        command = (os.environ.get("ORCA_CLI_COMMAND")
                   or ("orca-dev" if os.environ.get("ORCA_DEV_REPO_ROOT") else "orca"))
        orca = shutil.which(command)
        if not orca:
            raise OSError(f"Orca terminal requires {command} on PATH")
    tmux = shutil.which("tmux") if not orca and os.environ.get("TMUX") else None
    if not orca and not tmux:
        raise OSError("fa design open needs an Orca terminal or a tmux session")

    title = f"design: {root.name}"
    record_path = store.root / "design-sessions" / f"{uuid4()}.json"
    prompt = (f"Use fa-design-session. Speak in the project's language ({language}). "
              f"The session and preview terminal handles will be recorded in {record_path}. "
              "Read that file before using the preview pane. ")
    if topic is None:
        prompt += "Help choose the design topic from the current project and conversation."
    else:
        prompt += f"Initial topic: {json.dumps(topic, ensure_ascii=False)}"
    argv = [claude, "--append-system-prompt", _skill_source()]
    if (root / ".claude/agents/fa-design.md").is_file():
        argv += ["--agent", "fa-design"]
    argv.append(prompt)
    command = shlex.join(argv)
    preview = shlex.join(["/bin/sh", "-c",
                          'printf "%s\\n" "Design preview — ready for drafts"; '
                          'exec "${SHELL:-/bin/sh}"'])
    record = {"repo_root": str(root), "topic": topic, "title": title,
              "opened_at": store._timestamp(), "preview_handle": None}
    if orca:
        handle = _orca_handle(_request([
            orca, "terminal", "create", "--worktree", f"path:{root}",
            "--title", title, "--command", command, "--json",
        ], root))
        record.update(backend="orca", executable=orca, handle=handle)
    else:
        handle = _request([tmux, "new-window", "-P", "-F", "#{window_id}",
                           "-n", title, "-c", str(root), command], root)
        if not handle:
            raise OSError("tmux did not return a design window id")
        record.update(backend="tmux", executable=tmux, handle=handle)
    # Save before splitting: a failed preview must still leave the live tab discoverable.
    store._write_json(record_path, record)
    try:
        if orca:
            record["preview_handle"] = _orca_handle(_request([
                orca, "terminal", "split", "--terminal", handle,
                "--direction", "vertical", "--command", preview, "--json",
            ], root))
        else:
            record["preview_handle"] = _request([
                tmux, "split-window", "-h", "-t", handle, "-P", "-F", "#{pane_id}",
                "-c", str(root), preview,
            ], root)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        record["preview_error"] = str(exc)
        print(f"foragent: design tab opened; preview pane failed; see {record_path}", file=sys.stderr)
    store._write_json(record_path, record)
    print(f"Opened {title}\nDesign session: {record_path}")
    return record_path
