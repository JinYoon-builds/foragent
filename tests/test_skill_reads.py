"""Skill use comes from successful ordinary reads in the executor's own log (#1619).

Ordinary reads are the file read tool and a plain read command of a SKILL.md. Compound
commands, redirections and early exits are notes, not holds, so only their plain shape is fixed here.
"""

import json
import shlex

import pytest

from foragent.skill_reads import record, skills_read
from foragent.state import StateStore

SKILL = "/lib/skills/tone/SKILL.md"


def write_log(path, *events):
    path.write_text("".join(json.dumps(event) + "\n" for event in events))
    return path


def claude_call(tool, values, *, error=False, identity="a"):
    return ({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": identity, "name": tool, "input": values}]}},
            {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": identity, "is_error": error,
                                                      "content": "text"}]}})


def codex_call(command, code=0, status="completed"):
    return {"type": "item.completed", "item": {"type": "command_execution", "command": command, "exit_code": code,
                                               "aggregated_output": "text", "status": status}}


@pytest.mark.parametrize("command,expected", [
    # Plain read commands of a SKILL.md, as Codex runs them and as written by hand.
    (f"cat '{SKILL}'", ["tone"]),
    # Paths are resolved as written; any folder name a shell leaves alone is a skill name.
    ("cat /lib/skills/tone/../tone/SKILL.md", ["tone"]),
    ("cat /lib/skills/./tone//SKILL.md", ["tone"]),
    ("cat ./skills/tone/SKILL.md", ["tone"]),
    ("cat /lib/skills/-tone/SKILL.md", ["-tone"]),
    ("cat /lib/skills/.tone/SKILL.md", [".tone"]),
    ("cat '/lib/skills/my tone/SKILL.md'", ["my tone"]),
    ("cat /lib/skills/tön/SKILL.md", ["tön"]),
    ("cat /lib/skills/tone/./SKILL.md", ["tone"]),
    ("cat /lib/skills/../SKILL.md", []),
    ("cat '~/skills/tone/SKILL.md'", ["tone"]),  # A literal folder named ~; the name is literal.
    (f"/bin/zsh -lc \"cat '{SKILL}'\"", ["tone"]),
    (f"bash -lc \"sed -n '1,200p' {SKILL}\"", ["tone"]),
    (f"head -n 80 {SKILL}", ["tone"]),
    (f"nl -ba {SKILL}", ["tone"]),
    ("cat skills/tone/SKILL.md", ["tone"]),
    (f"cat {SKILL} /lib/skills/voice/SKILL.md", ["tone", "voice"]),
    # Option values and a sed script are not files, however they are written.
    (f"sed -e '# /missing/skills/phantom/SKILL.md' {SKILL}", ["tone"]),
    (f"sed '# /missing/skills/phantom/SKILL.md' {SKILL}", ["tone"]),
    (f"sed -n -e 1,80p {SKILL}", ["tone"]),
    (f"sed -f /missing/skills/phantom/SKILL.md notes.txt", []),
    (f"nl -s /missing/skills/phantom/SKILL.md {SKILL}", ["tone"]),
    (f"head -n /missing/skills/phantom/SKILL.md {SKILL}", ["tone"]),
    (f"head -20 {SKILL}", ["tone"]),
    (f"cat -- {SKILL}", ["tone"]),
    (f"cat --help {SKILL}", []),
    (f"less {SKILL}", []),
    # GNU and BSD options of ordinary reads still count.
    (f"sed -l -n '1,80p' {SKILL}", ["tone"]),
    (f"sed -u -s -n 1,80p {SKILL}", ["tone"]),
    (f"sed --quiet -e 1,80p {SKILL}", ["tone"]),
    (f"sed -n --expression=1,80p {SKILL}", ["tone"]),
    (f"sed -n --expression /missing/skills/phantom/SKILL.md {SKILL}", ["tone"]),
    (f"head --lines=80 {SKILL}", ["tone"]),
    (f"head --lines 80 {SKILL}", ["tone"]),
    (f"tail -n +1 -q {SKILL}", ["tone"]),
    (f"cat --number -A {SKILL}", ["tone"]),
    (f"nl --body-numbering=a {SKILL}", ["tone"]),
    (f"sed --in-place s/a/b/ {SKILL}", []),
    (f"sed -I '' s/a/b/ {SKILL}", []),
    (f"head --help {SKILL}", []),
    (f"cat --number=3 {SKILL}", []),
    (f"cat -Z {SKILL}", []),
    (f"sed -i.bak 1d {SKILL}", []),
    (f"sed -n 1p '' {SKILL}", ["tone"]),
    # Not a plain read: a mention, a search, an edit, or a compound command.
    (f"echo {SKILL}", []),
    (f"grep tone {SKILL}", []),
    (f"sed -i '' s/a/b/ {SKILL}", []),
    (f"cat {SKILL}; true || cat /lib/skills/voice/SKILL.md", []),
    (f"cat {SKILL} > copy.txt", []),
    # Not a skill: another file, a pattern or a variable.
    ("cat docs/SKILL.md", []),
    ("cat SKILL.md", []),
    (f"cat {SKILL}.bak", []),
    ("cat /lib/skills/*/SKILL.md", []),
    ("cat /lib/skills/$NAME/SKILL.md", []),
    ("cat 'unbalanced", []),
])
def test_command_reads(tmp_path, command, expected):
    assert skills_read(write_log(tmp_path / "log", codex_call(command))) == expected


def test_codex_needs_exit_zero_and_a_completed_item(tmp_path):
    log = write_log(tmp_path / "log", codex_call(f"cat {SKILL}", code=1), codex_call(f"cat {SKILL}", status="failed"))
    assert skills_read(log) == []


def test_claude_read_tool_counts_once_it_succeeds(tmp_path):
    log = write_log(tmp_path / "log",
                    *claude_call("Read", {"file_path": SKILL, "offset": 40, "limit": 10}),
                    *claude_call("Read", {"file_path": "/lib/skills/failed/SKILL.md"}, error=True, identity="b"),
                    *claude_call("Read", {"file_path": "/lib/feature.py"}, identity="c"),
                    claude_call("Read", {"file_path": "/lib/skills/unanswered/SKILL.md"}, identity="d")[0],
                    {"type": "assistant", "message": {"content": [{"type": "text", "text": "/lib/skills/said/SKILL.md"}]}})
    assert skills_read(log) == ["tone"]


def test_claude_shell_reads_follow_the_same_plain_rule(tmp_path):
    log = write_log(tmp_path / "log",
                    *claude_call("Bash", {"command": f"cat {SKILL}"}),
                    *claude_call("Bash", {"command": "echo /lib/skills/voice/SKILL.md"}, identity="b"),
                    *claude_call("Bash", {"command": "cat /lib/skills/failed/SKILL.md"}, error=True, identity="c"),
                    *claude_call("Skill", {"skill": "other"}, identity="d"))
    assert skills_read(log) == ["tone"]


def test_a_tool_call_needs_its_own_result_from_the_right_side(tmp_path):
    call, answer = claude_call("Read", {"file_path": SKILL})
    other = {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "other", "content": "x"}]}}
    assert skills_read(write_log(tmp_path / "a", call, other)) == []
    assert skills_read(write_log(tmp_path / "b", call, {"type": "assistant", "message": answer["message"]})) == []
    assert skills_read(write_log(tmp_path / "c", {"type": "user", "message": call["message"]}, answer)) == []
    assert skills_read(write_log(tmp_path / "d", *claude_call("Read", {"file_path": SKILL}, identity=None))) == []


def test_repeated_reads_record_once_and_only_after_the_offset(tmp_path):
    log = write_log(tmp_path / "log", codex_call(f"cat {SKILL}"), codex_call(f"cat {SKILL}"))
    assert skills_read(log) == ["tone"]
    offset = log.stat().st_size
    with log.open("a") as stream:
        stream.write(json.dumps(codex_call("cat /lib/skills/later/SKILL.md")) + "\n")
    assert skills_read(log, offset) == ["later"]


def test_record_adds_to_earlier_attempts_and_says_nothing_without_reads(tmp_path):
    store = StateStore(tmp_path / "state")
    store.start_run(1, branch="b", worktree=tmp_path / "w")
    record(store, 1, tmp_path / "missing", 0)
    record(store, 1, write_log(tmp_path / "empty", codex_call("ls")), 0)
    assert not store.get_run(1).skills_reported
    record(store, 1, write_log(tmp_path / "one", codex_call(f"cat {SKILL}")), 0)
    record(store, 1, write_log(tmp_path / "two", codex_call("cat /lib/skills/voice/SKILL.md"), codex_call(f"cat {SKILL}")), 0)
    run = store.get_run(1)
    assert run.skills_reported and run.skills_used == ["tone", "voice"]


@pytest.mark.parametrize("path,expected", [
    ("/lib/skills/tone/../tone/SKILL.md", ["tone"]),
    ("/lib/skills/-tone/SKILL.md", ["-tone"]),
    ("/lib/skills/tone/../SKILL.md", []),
])
def test_claude_read_paths_resolve_as_written(tmp_path, path, expected):
    assert skills_read(write_log(tmp_path / "log", *claude_call("Read", {"file_path": path}))) == expected


@pytest.mark.parametrize("path,expected", [
    ("/project [2026]/skills/example/SKILL.md", ["example"]),
    ("/home/*me*/skills/example/SKILL.md", ["example"]),
    ("/lib/skills/[x]/SKILL.md", ["[x]"]),
    ("/lib/skills/$name/SKILL.md", ["$name"]),
])
def test_the_read_tool_takes_its_path_literally(tmp_path, path, expected):
    assert skills_read(write_log(tmp_path / "log", *claude_call("Read", {"file_path": path}))) == expected


@pytest.mark.parametrize("command,expected", [
    ("cat '/project [2026]/skills/example/SKILL.md'", ["example"]),
    ("sed -n 1,80p \"/project [2026]/skills/example/SKILL.md\"", ["example"]),
    ("/bin/zsh -lc \"cat '/project [2026]/skills/example/SKILL.md'\"", ["example"]),
    ("cat /project*/skills/example/SKILL.md", ["example"]),
    ("cat /lib/skills/*/SKILL.md", []),
    ("cat /lib/skills/$NAME/SKILL.md", []),
])
def test_shell_reads_need_only_a_literal_skill_name(tmp_path, command, expected):
    log = write_log(tmp_path / "log", codex_call(command),
                    *claude_call("Bash", {"command": command}, identity="b"))
    assert skills_read(log) == expected


@pytest.mark.parametrize("command,expected", [
    # Quotes and escapes keep characters ordinary, as the shell does.
    ("cat /project#2026/skills/example/SKILL.md", ["example"]),
    ("cat '/lib/skills/[example]/SKILL.md'", ["[example]"]),
    ('cat "/lib/skills/[example]/SKILL.md"', ["[example]"]),
    ("cat /lib/skills/\\[example\\]/SKILL.md", ["[example]"]),
    ("sed ';' skills/example/SKILL.md", ["example"]),
    ("sed -e '1,80p;q' -n skills/example/SKILL.md", ["example"]),
    ('sed "s/a/b/" "skills/ex ample/SKILL.md"', ["ex ample"]),
    ('cat "$HOME/skills/example/SKILL.md"', ["example"]),
    ("cat skills/example/SKILL.md # read it", ["example"]),
    ("cat 'skills/example/SKILL.md' '#2'", ["example"]),
    # Unquoted, the shell would expand the name or join commands: no plain read of one skill.
    ("cat /lib/skills/[example]/SKILL.md", []),
    ('cat "/lib/skills/$NAME/SKILL.md"', []),
    ("cat /lib/skills/{a,b}/SKILL.md", []),
    ("cat skills/example/SKILL.md;", []),
    ("cat skills/example/SKILL.md\ncat x", []),
    ("cat 'skills/example/SKILL.md", []),
    ("# cat skills/example/SKILL.md", []),
])
def test_quoting_decides_what_the_shell_sees(tmp_path, command, expected):
    log = write_log(tmp_path / "log", codex_call(command),
                    *claude_call("Bash", {"command": command}, identity="b"),
                    codex_call(f"/bin/bash -lc {shlex.quote(command)}"))
    assert skills_read(log) == expected
