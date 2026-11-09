"""Shared world for the planning-session memory-write reminder (Jin r1315).

`fa init` runs in a temporary project with an isolated HOME (fake tracker and git, no
network, no real Claude). Then every PreToolUse hook found in the Claude Code settings a
planning session reads (project .claude/settings.json, .claude/settings.local.json and the
user's ~/.claude/settings.json) is run the way Claude Code runs it: the hook JSON on stdin,
and stdout read back as hook output.

The reminder rides on the planning-session hooks consent (`fa init --decision-hooks yes`,
PM answer), which already writes the decision hooks (`fa decisions stop-check`,
`fa decisions _sweep`) into the project's .claude/settings.local.json. `init()` gives that
consent and, by default, every other Claude-settings consent too (`--statusline yes`,
`--fa-allow yes`, prompts answered yes); `init(decision_hooks=..., others=...)` varies them.
"""
from io import BytesIO, TextIOWrapper
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys

import yaml

from foragent import cli
from foragent.init import init_repo
from foragent.tracker import FakeTracker
from test_fa_init_allow import SetupGit
from test_skills_autosync import cli_exit

REPO = "example/project"
SESSION = "5b1f7a2c-planning-session"
COMMANDS = ("fa order note --kind decision", "fa orch promise add", "fa order note --kind idea")
WRITE_TOOLS = ("Write", "Edit")


def slug(path):
    return re.sub(r"[^A-Za-z0-9]", "-", str(path))


class Case:
    def __init__(self, tmp_path, monkeypatch):
        home, root, bin_dir = tmp_path / "home", tmp_path / "project", tmp_path / "bin"
        for folder in (home, root, bin_dir):
            folder.mkdir()
        self.home, self.root, self.patch = home, root, monkeypatch
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setattr(Path, "home", lambda: home)
        for key in ("CLAUDE_CONFIG_DIR", "FORAGENT_ROLE", "FORAGENT_SESSION_ID", "CLAUDE_ENV_FILE"):
            monkeypatch.delenv(key, raising=False)
        for key in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"):
            monkeypatch.setenv(key, str(home / key.lower()))
        monkeypatch.setenv("FORAGENT_VIEW", "off")
        monkeypatch.setenv("FORAGENT_UPDATE_CHECK", "off")
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(root))
        monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
        monkeypatch.setattr(cli, "kick_tick", lambda *args: None)
        (root / ".foragent.yml").write_text(yaml.safe_dump({
            "repo": REPO, "checks": "local", "executor": "claude", "language": "en",
            "skill_libraries": [],
        }), encoding="utf-8")
        (home / ".claude").mkdir()
        (home / ".claude/settings.json").write_text("{}\n", encoding="utf-8")
        tracker = FakeTracker(login="human")
        monkeypatch.setattr(cli, "init_repo", lambda root_arg, **kwargs: init_repo(
            root, git=SetupGit(root), tracker=tracker, gh_run=lambda args, input=None: "", **kwargs))
        monkeypatch.setattr("builtins.input", lambda prompt="": "yes")
        self.settings = [root / ".claude/settings.json", root / ".claude/settings.local.json",
                         home / ".claude/settings.json"]
        self.transcript = home / ".claude/projects" / slug(root) / f"{SESSION}.jsonl"
        self.transcript.parent.mkdir(parents=True)
        self.transcript.write_text("", encoding="utf-8")
        # The planning session's own Claude Code memory folder.
        self.memory_dir = home / ".claude/projects" / slug(root) / "memory"

    def init(self, decision_hooks="yes", others="yes"):
        argv = ["init", "--repo-root", str(self.root), "--no-push", "--no-launchd", "--no-board",
                "--statusline", others, "--starter", "no", "--fa-allow", others,
                "--decision-hooks", decision_hooks]
        with self.patch.context() as patch:
            patch.setattr("builtins.input", lambda prompt="": others)
            assert cli_exit(cli.main, argv) == 0, "fa init succeeds in the temporary project"

    def local_settings(self):
        path = self.root / ".claude/settings.local.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}

    def decision_hook_commands(self):
        """Commands of the existing decision hooks in the project's settings.local.json."""
        return [hook.get("command", "") for groups in (self.local_settings().get("hooks") or {}).values()
                for group in groups for hook in group.get("hooks") or []
                if "fa decisions" in hook.get("command", "")]

    def pre_tool_use_hooks(self, tool_name="Write"):
        """(settings file, command) for every PreToolUse hook that would run for this tool."""
        found = []
        for path in self.settings:
            try:
                settings = json.loads(path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                continue
            for group in (settings.get("hooks") or {}).get("PreToolUse") or []:
                if not _matches(group.get("matcher", ""), tool_name):
                    continue
                for hook in group.get("hooks") or []:
                    if hook.get("type") == "command" and hook.get("command"):
                        found.append((path, hook["command"]))
        return found

    def run_hook(self, command, payload):
        """Run one hook command as Claude Code would; return (exit code, stdout, stderr)."""
        data = json.dumps(payload).encode("utf-8")
        argv = shlex.split(command)
        while argv and "=" in argv[0]:
            argv = argv[1:]
        if argv and Path(argv[0]).name == "fa":
            streams = [TextIOWrapper(BytesIO(raw), encoding="utf-8") for raw in (data, b"", b"")]
            with self.patch.context() as patch:
                patch.setattr(sys, "stdin", streams[0])
                patch.setattr(sys, "stdout", streams[1])
                patch.setattr(sys, "stderr", streams[2])
                patch.chdir(self.root)
                code = cli_exit(cli.main, argv[1:])
            outputs = []
            for stream in streams[1:]:
                stream.flush()
                outputs.append(stream.buffer.getvalue().decode("utf-8", "replace"))
            return (0 if code is None else code), outputs[0], outputs[1]
        done = subprocess.run(command, shell=True, input=data, capture_output=True, cwd=self.root,
                              env=dict(os.environ), timeout=60)
        return done.returncode, done.stdout.decode("utf-8", "replace"), done.stderr.decode("utf-8", "replace")

    def payload(self, file_path, tool_name="Write", content="Short note."):
        tool_input = ({"file_path": str(file_path), "content": content} if tool_name == "Write"
                      else {"file_path": str(file_path), "old_string": "old", "new_string": content})
        return {"session_id": SESSION, "transcript_path": str(self.transcript), "cwd": str(self.root),
                "permission_mode": "default", "hook_event_name": "PreToolUse",
                "tool_name": tool_name, "tool_input": tool_input}

    def contexts(self, file_path, tool_name="Write", content="Short note."):
        """Context every installed PreToolUse hook adds for one write, one entry per hook output."""
        added = []
        for _path, command in self.pre_tool_use_hooks(tool_name):
            code, out, err = self.run_hook(command, self.payload(file_path, tool_name, content))
            assert code == 0, f"A PreToolUse hook never blocks or disturbs the write: {command} -> {code} {err}"
            text = additional_context(out)
            if text:
                added.append(text)
        return added


def additional_context(stdout):
    """The text a PreToolUse hook's output adds to the session's context (Claude Code hook JSON)."""
    stdout = stdout.strip()
    if not stdout:
        return ""
    try:
        value = json.loads(stdout)
    except ValueError:
        return ""
    if not isinstance(value, dict):
        return ""
    specific = value.get("hookSpecificOutput")
    if isinstance(specific, dict) and specific.get("hookEventName") in (None, "PreToolUse"):
        context = specific.get("additionalContext")
        if isinstance(context, str):
            return context
    return ""


def _matches(matcher, value):
    return not matcher or matcher == "*" or value in matcher.split("|") or bool(re.fullmatch(matcher, value or ""))
