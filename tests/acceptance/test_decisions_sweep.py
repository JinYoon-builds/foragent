"""Locked acceptance: decisions in a planning conversation are swept before they can be lost (#1499).

Contract declared by this issue:
- `fa init --decision-hooks yes` installs Claude Code hooks (rerunning keeps one copy). An
  explicit flag, because merged locks pin the init prompts and the Claude settings init
  writes without consent.
- Before compaction, at session end and after a set amount of new person messages in a
  planning session, the hooks start one fresh Claude session (not a resume of the person's
  session) that is given the transcript (its path or the person's new words). A part of the
  conversation that was already swept is not swept again.
- That session is a session Foragent launches (FORAGENT_ROLE is set), so its own hooks never
  start another sweep. A decision candidate it appends with `fa order note` keeps the
  source: the transcript it came from.
Only a fake `claude` runs here; what counts as a decision is the fresh session's judgment and
is not tested. Pinned: the Claude Code hook events, the launch facts above and `fa order
note`/`fa order pending --json`. The amount of messages, prompt, launch flags, where the
sweep position is kept and the hook command are the lane's choice.
"""
from io import BytesIO, TextIOWrapper
import json
import os
from pathlib import Path
import re
import shlex
import stat
import subprocess
import sys
import time

import pytest
import yaml

from foragent import cli
from foragent.init import init_repo
from foragent.tracker import FakeTracker
from test_fa_init_allow import SetupGit
from test_skills_autosync import cli_exit

REPO = "example/project"
SESSION = "6f1c0d2e-pm-session"
FIRST = "Let's keep the night runs stopping at six, every lane."
SECOND = "And reports go out on Fridays from now on."
FRESH_FLAGS = {"--resume", "-r", "--continue", "-c"}

FAKE = """#!{python}
import json, os, select, subprocess, sys
data = ""
try:
    ready, _, _ = select.select([sys.stdin], [], [], 0.5)
    if ready:
        data = sys.stdin.read()
except Exception:
    pass
record = {{"argv": sys.argv[1:], "stdin": data, "role": os.environ.get("FORAGENT_ROLE")}}
quote = {quote!r}
if quote:
    # Behave like the sweep session: append the candidate it found, as the Goal says.
    done = subprocess.run([{python!r}, "-m", "foragent.cli", "order", "note", "--defer", "decision-sweep",
                           "--kind", "decision", "--text", quote, "--repo-root", {root!r}],
                          capture_output=True, text=True, env={{**os.environ, **{env!r}}})
    record["note"] = [done.returncode, done.stderr[-400:]]
with open({log!r}, "a", encoding="utf-8") as stream:
    stream.write(json.dumps(record) + "\\n")
"""


@pytest.fixture
def project(tmp_path, monkeypatch):
    home, root, bin_dir = tmp_path / "home", tmp_path / "project", tmp_path / "bin"
    for folder in (home, root, bin_dir):
        folder.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: home)
    for key in ("CLAUDE_CONFIG_DIR", "FORAGENT_ROLE", "FORAGENT_SESSION_ID", "CLAUDE_ENV_FILE"):
        monkeypatch.delenv(key, raising=False)
    env = {"HOME": str(home)}
    for key in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"):
        monkeypatch.setenv(key, str(home / key.lower()))
        env[key] = str(home / key.lower())
    monkeypatch.setenv("FORAGENT_VIEW", "off")
    monkeypatch.setenv("FORAGENT_UPDATE_CHECK", "off")
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(cli, "kick_tick", lambda *args: None)
    (root / ".foragent.yml").write_text(yaml.safe_dump({
        "repo": REPO, "checks": "local", "executor": "claude", "language": "en", "skill_libraries": [],
    }), encoding="utf-8")
    (home / ".claude").mkdir()
    (home / ".claude/settings.json").write_text("{}\n", encoding="utf-8")
    tracker = FakeTracker(login="human")
    monkeypatch.setattr(cli, "init_repo", lambda root_arg, **kwargs: init_repo(
        root, git=SetupGit(root), tracker=tracker, gh_run=lambda args, input=None: "", **kwargs))
    monkeypatch.setattr("builtins.input", lambda prompt="": "yes")  # The person also accepts if asked.

    class Case:
        pass

    case = Case()
    case.root, case.home, case.patch = root, home, monkeypatch
    case.log = tmp_path / "claude-launches.jsonl"
    case.transcript = home / ".claude/projects" / re.sub(r"[^A-Za-z0-9]", "-", str(root)) / f"{SESSION}.jsonl"
    case.transcript.parent.mkdir(parents=True)
    case.transcript.write_text("", encoding="utf-8")
    case.settings = [root / ".claude/settings.json", root / ".claude/settings.local.json",
                     home / ".claude/settings.json"]

    def fake_claude(quote=""):
        path = bin_dir / "claude"
        path.write_text(FAKE.format(python=sys.executable, quote=quote, root=str(root), env=env,
                                    log=str(case.log)), encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    def install(quote=""):
        fake_claude(quote)
        argv = ["init", "--repo-root", str(root), "--no-push", "--no-launchd", "--no-board",
                "--statusline", "no", "--starter", "no", "--fa-allow", "no", "--decision-hooks", "yes"]
        assert cli_exit(cli.main, argv) == 0, "fa init --decision-hooks yes installs the sweep hooks"
        case.log.write_text("", encoding="utf-8")  # Count only what the hooks start from here.

    case.install = install
    return case


def say(case, words):
    """One person message and one reply land in the planning transcript."""
    with case.transcript.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"type": "user", "sessionId": SESSION,
                                 "message": {"role": "user", "content": words}}) + "\n")
        stream.write(json.dumps({"type": "assistant", "sessionId": SESSION, "message": {
            "role": "assistant", "content": [{"type": "text", "text": "Noted."}]}}) + "\n")


def _matches(matcher, value):
    return not matcher or matcher == "*" or value in matcher.split("|") or re.fullmatch(matcher, value or "")


def hook(case, event, **fields):
    """Run every installed hook for one Claude Code event, as Claude Code would."""
    payload = {"session_id": SESSION, "transcript_path": str(case.transcript), "cwd": str(case.root),
               "hook_event_name": event, **fields}
    value = fields.get("trigger") or fields.get("reason") or fields.get("source") or ""
    text = json.dumps(payload).encode("utf-8")
    for path in case.settings:
        try:
            settings = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            continue
        for group in (settings.get("hooks") or {}).get(event) or []:
            if not _matches(group.get("matcher", ""), value):
                continue
            for command in (h["command"] for h in group.get("hooks", []) if h.get("type") == "command"):
                argv = shlex.split(command)
                while argv and "=" in argv[0]:
                    argv = argv[1:]
                if argv and Path(argv[0]).name == "fa":
                    streams = [TextIOWrapper(BytesIO(data), encoding="utf-8") for data in (text, b"", b"")]
                    with case.patch.context() as patch:
                        patch.setattr(sys, "stdin", streams[0])
                        patch.setattr(sys, "stdout", streams[1])
                        patch.setattr(sys, "stderr", streams[2])
                        patch.chdir(case.root)
                        code = cli_exit(cli.main, argv[1:])
                else:
                    code = subprocess.run(command, shell=True, input=text, capture_output=True, cwd=case.root,
                                          env=dict(os.environ), timeout=60).returncode
                assert code == 0, f"A {event} hook never disturbs the person's session: {command} -> {code}"


def launches(case, expected=None, wait=8.0):
    """Fake claude launches so far; waits for ``expected`` launches, or the whole ``wait`` for none more."""
    deadline = time.monotonic() + wait
    while True:
        rows = [json.loads(line) for line in case.log.read_text(encoding="utf-8").splitlines() if line.strip()]
        if expected is not None and len(rows) >= expected or time.monotonic() > deadline:
            return rows
        time.sleep(0.05)


def assert_fresh_sweep(case, launch, words):
    argv = launch["argv"]
    assert not FRESH_FLAGS & set(argv) and SESSION not in argv, f"A fresh session, not the person's: {argv}"
    given = " ".join(argv) + launch["stdin"]
    assert str(case.transcript) in given or words in given, "The sweep session is given the transcript"
    assert launch["role"], "The sweep session is one Foragent launches, so its hooks start nothing"


def test_compaction_and_session_end_sweep_only_the_new_part(project):
    case = project
    case.install()
    say(case, FIRST)
    hook(case, "PreCompact", trigger="auto", custom_instructions="")
    rows = launches(case, 1)
    assert len(rows) == 1, "Compaction in a planning session starts one sweep"
    assert_fresh_sweep(case, rows[0], FIRST)
    hook(case, "SessionEnd", reason="clear")
    assert len(launches(case, wait=3.0)) == 1, "Nothing new since the last sweep: no second sweep"
    say(case, SECOND)
    hook(case, "SessionEnd", reason="clear")
    rows = launches(case, 2)
    assert len(rows) == 2, "Session end sweeps the person's new words"
    assert_fresh_sweep(case, rows[1], SECOND)
    say(case, "One more thought for the sweep session itself.")
    case.patch.setenv("FORAGENT_ROLE", "launched")
    hook(case, "PreCompact", trigger="auto", custom_instructions="")
    hook(case, "SessionEnd", reason="other")
    assert len(launches(case, wait=3.0)) == 2, "A session Foragent launched never starts a sweep"


def test_person_messages_start_a_sweep_after_a_set_amount(project):
    case = project
    case.install()
    say(case, "Message 1")
    hook(case, "UserPromptSubmit", prompt="Message 1")
    assert launches(case, wait=3.0) == [], "One message is not yet a sweep"
    sent = 1
    while not launches(case, 1, wait=0.5) and sent < 50:
        sent += 1
        say(case, f"Message {sent}")
        hook(case, "UserPromptSubmit", prompt=f"Message {sent}")
    rows = launches(case, 1)
    assert len(rows) == 1, f"A set amount (at most 50) of new person messages starts one sweep; sent {sent}"
    assert_fresh_sweep(case, rows[0], f"Message {sent}")
    say(case, f"Message {sent + 1}")
    hook(case, "UserPromptSubmit", prompt=f"Message {sent + 1}")
    assert len(launches(case, wait=3.0)) == 1, "The count starts again after a sweep"


def test_a_candidate_from_the_sweep_keeps_its_source(project, capsys):
    case = project
    case.install(quote=FIRST)
    say(case, FIRST)
    hook(case, "PreCompact", trigger="manual", custom_instructions="")
    rows = launches(case, 1)
    assert len(rows) == 1 and rows[0]["note"][0] == 0, f"The sweep session appends with fa order note: {rows}"
    capsys.readouterr()
    assert cli_exit(cli.main, ["order", "pending", "--json", "--repo-root", str(case.root)]) == 0
    notes = [row for row in json.loads(capsys.readouterr().out)["notes"] if row.get("text") == FIRST]
    assert len(notes) == 1, "One candidate, in the person's words"
    assert str(case.transcript) in json.dumps(notes[0], ensure_ascii=False), (
        f"The candidate keeps where it came from: {notes[0]}")
