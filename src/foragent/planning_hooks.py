"""Planning-session hooks `fa init --decision-hooks yes` adds beside the decision hooks (r1315).

The memory-write reminder: when the planning session is about to write one of its memory files
(any */memory/*.md), Claude Code adds a line naming the FA commands that take decisions,
promises and order ideas, so they reach FA instead of staying in memory. The hook judges the
act of writing that file only, never the words. Its one writer of settings is ``install``.
"""

import json
from pathlib import Path, PurePath
import shlex
import sys

from foragent.i18n import text

EVENT = "PreToolUse"
MATCHER = "Write|Edit|MultiEdit"
ARGUMENTS = ["decisions", "_memory-write"]


def install(root: Path) -> None:
    """Add the reminder to the project's .claude/settings.local.json once, keeping everything else."""
    from foragent import statusline
    from foragent.config import load_config
    from foragent.order import PlanError

    path = root / ".claude/settings.local.json"
    # Same lock as the decision hook installers: all of them write this file during `fa init`.
    with statusline._install_lock():
        try:
            settings = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
            if not isinstance(settings, dict):
                raise ValueError
            hooks = settings.setdefault("hooks", {})
            groups = hooks.setdefault(EVENT, []) if isinstance(hooks, dict) else None
            if not isinstance(groups, list):
                raise ValueError
            if any(isinstance(hook, dict) and statusline._fa_arguments(str(hook.get("command", ""))) == ARGUMENTS
                   for group in groups if isinstance(group, dict) for hook in group.get("hooks") or []):
                return
            groups.append({"matcher": MATCHER, "hooks": [
                {"type": "command", "command": shlex.join(["fa", *ARGUMENTS]), "timeout": 10}]})
        except (ValueError, AttributeError, TypeError) as exc:
            raise PlanError(text("planning_hooks_settings", load_config(root).language, path=path)) from exc
        statusline._write_json(path, settings)


def is_memory_file(file_path: str) -> bool:
    """True for a path matching */memory/*.md."""
    path = PurePath(file_path)
    return path.suffix == ".md" and path.parent.name == "memory"


def memory_write(stdin=None, stdout=None) -> int:
    """PreToolUse hook: remind on a memory-file write, print nothing otherwise; never block."""
    stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
    try:
        payload = json.load(stdin)
        tool_input = payload.get("tool_input") if isinstance(payload, dict) else None
        file_path = tool_input.get("file_path") if isinstance(tool_input, dict) else None
        if not isinstance(file_path, str) or not is_memory_file(file_path):
            return 0
        from foragent.cli import _project_language
        cwd = payload.get("cwd")
        language = _project_language(Path(cwd) if isinstance(cwd, str) and cwd else None)
    except Exception:  # A reminder must never disturb the write it rides on.
        return 0
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": EVENT, "additionalContext": text("planning_memory_write_reminder", language),
    }}, ensure_ascii=False), file=stdout)
    return 0
