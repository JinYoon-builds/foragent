"""Skill use comes from successful reads in the executor's own log (#1619).

A read needs the file's own opening lines in the call's output, so these cases check what the
output shows, not what a command looks like.
"""

import json

import pytest

from foragent.skill_reads import record, skills_read
from foragent.state import StateStore

TEXT = "---\nname: tone\ndescription: Check the tone of release notes\n---\nUse a calm voice.\n"
NOTES = "Plain notes.\n"


@pytest.fixture
def files(tmp_path):
    skill = tmp_path / "lib/skills/tone/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(TEXT)
    other = tmp_path / "lib/skills/voice/SKILL.md"
    other.parent.mkdir(parents=True)
    other.write_text(TEXT.replace("tone", "voice"))
    (tmp_path / "notes.txt").write_text(NOTES)
    return tmp_path


def write_log(path, *events):
    path.write_text("".join(json.dumps(event) + "\n" for event in events))
    return path


def claude_call(tool, values, output, *, error=False, identity="a"):
    return ({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": identity, "name": tool, "input": values}]}},
            {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": identity, "is_error": error, "content": output}]}})


def codex_call(command, output, code=0, status="completed"):
    return {"type": "item.completed", "item": {"type": "command_execution", "command": command, "exit_code": code,
                                               "aggregated_output": output, "status": status}}


def numbered(text):
    return "".join(f"{n:6}\t{line}\n" for n, line in enumerate(text.splitlines(), 1))


SKILL = "{root}/lib/skills/tone/SKILL.md"
VOICE = "{root}/lib/skills/voice/SKILL.md"


@pytest.mark.parametrize("command,output,expected", [
    # Real reads: the output carries the file's text, however the command was written.
    (f"cat '{SKILL}'", TEXT, ["tone"]),
    (f"sed -n '1,200p' {SKILL}", TEXT, ["tone"]),
    (f"/bin/zsh -lc \"cat '{SKILL}'\"", TEXT, ["tone"]),
    (f"cat -n {SKILL}", numbered(TEXT), ["tone"]),
    (f"ls; cat {SKILL} | head -20", "x\n" + TEXT, ["tone"]),
    (f"cat {SKILL} {VOICE}", TEXT + TEXT.replace("tone", "voice"), ["tone", "voice"]),
    ("cat lib/skills/tone/SKILL.md", TEXT, ["tone"]),
    # The command never consumed the file, though it names it and exits 0.
    (f"true || cat {SKILL}", "", []),
    (f"echo \"&&\" cat /missing/skills/tone/SKILL.md", "&& cat /missing/skills/tone/SKILL.md\n", []),
    (f"cat /dev/null < {SKILL}", "", []),
    (f"sed q {{root}}/notes.txt {SKILL}", NOTES, []),
    (f"sed -n 1p {SKILL}", "---\n", []),
    (f"head -n 0 {SKILL}", "", []),
    (f"cat notes.txt > {SKILL}", "", []),
    (f"cat {SKILL} > copy.txt", "", []),
    (f"cat {SKILL} 2>/dev/null >&3", "", []),
    (f"echo {SKILL}", f"{SKILL}\n", []),
    (f"cat --help {SKILL}", "Usage: cat [OPTION]... [FILE]...\n", []),
    # A missing file has no text to show; a pattern or variable names no skill.
    ("cat /lib/skills/gone/SKILL.md", TEXT, []),
    ("cat {root}/lib/skills/*/SKILL.md", TEXT, []),
    ("cat {root}/lib/skills/$NAME/SKILL.md", TEXT, []),
    ("cat {root}/notes.txt", NOTES, []),
])
def test_command_reads(files, command, output, expected):
    command = command.replace("{root}", str(files))
    output = output.replace("{root}", str(files))
    log = write_log(files / "log", codex_call(command, output))
    assert skills_read(log, worktree=files) == expected


def test_codex_needs_exit_zero_and_a_completed_item(files):
    command = f"cat {files}/lib/skills/tone/SKILL.md"
    log = write_log(files / "log", codex_call(command, TEXT, code=1), codex_call(command, TEXT, status="failed"))
    assert skills_read(log) == []


def test_a_relative_path_needs_the_executors_worktree(files):
    log = write_log(files / "log", codex_call("cat lib/skills/tone/SKILL.md", TEXT))
    assert skills_read(log) == [] and skills_read(log, worktree=files) == ["tone"]


def test_claude_read_needs_a_result_without_an_error(files):
    path = str(files / "lib/skills/tone/SKILL.md")
    log = write_log(files / "log",
                    *claude_call("Read", {"file_path": path}, numbered(TEXT)),
                    # The output even shows the text: the error flag alone voids the read.
                    *claude_call("Read", {"file_path": str(files / "lib/skills/voice/SKILL.md")},
                                 TEXT.replace("tone", "voice"), error=True, identity="b"),
                    claude_call("Read", {"file_path": path}, TEXT, identity="c")[0],
                    {"type": "assistant", "message": {"content": [{"type": "text", "text": path}]}})
    assert skills_read(log) == ["tone"]


def test_claude_result_blocks_and_shell_calls_count_by_their_output(files):
    path = str(files / "lib/skills/tone/SKILL.md")
    blocks = [{"type": "text", "text": TEXT}]
    log = write_log(files / "log",
                    *claude_call("Read", {"file_path": path}, blocks),
                    *claude_call("Bash", {"command": f"cat {files}/lib/skills/voice/SKILL.md"},
                                 TEXT.replace("tone", "voice"), identity="b"),
                    *claude_call("Bash", {"command": f"true || cat {path}"}, "", identity="c"))
    assert skills_read(log) == ["tone", "voice"]


def test_claude_skill_tool_shows_no_file_text_so_it_proves_nothing(files):
    log = write_log(files / "log", *claude_call("Skill", {"skill": "tone"}, "Launching skill: tone"))
    assert skills_read(log) == []


def test_a_tool_call_needs_its_own_result_from_the_right_side(files):
    call, answer = claude_call("Read", {"file_path": str(files / "lib/skills/tone/SKILL.md")}, TEXT)
    other = {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "other", "content": TEXT}]}}
    assert skills_read(write_log(files / "a", call, other)) == []
    assert skills_read(write_log(files / "b", call, {"type": "assistant", "message": answer["message"]})) == []
    assert skills_read(write_log(files / "c", {"type": "user", "message": call["message"]}, answer)) == []
    unnamed = claude_call("Read", {"file_path": str(files / "lib/skills/tone/SKILL.md")}, TEXT, identity=None)
    assert skills_read(write_log(files / "d", *unnamed)) == []


def test_repeated_reads_record_once_and_only_after_the_offset(files):
    command = f"cat {files}/lib/skills/tone/SKILL.md"
    log = write_log(files / "log", codex_call(command, TEXT), codex_call(command, TEXT))
    assert skills_read(log) == ["tone"]
    offset = log.stat().st_size
    with log.open("a") as stream:
        stream.write(json.dumps(codex_call(f"cat {files}/lib/skills/voice/SKILL.md", TEXT.replace("tone", "voice"))) + "\n")
    assert skills_read(log, offset) == ["voice"]


def test_record_adds_to_earlier_attempts_and_says_nothing_without_reads(files):
    store = StateStore(files / "state")
    store.start_run(1, branch="b", worktree=files / "w")
    record(store, 1, files / "missing", 0)
    record(store, 1, write_log(files / "empty", codex_call("ls", "notes.txt\n")), 0)
    assert not store.get_run(1).skills_reported
    tone = codex_call(f"cat {files}/lib/skills/tone/SKILL.md", TEXT)
    voice = codex_call("cat lib/skills/voice/SKILL.md", TEXT.replace("tone", "voice"))
    record(store, 1, write_log(files / "one", tone), 0)
    record(store, 1, write_log(files / "two", voice, tone), 0, files)
    run = store.get_run(1)
    assert run.skills_reported and run.skills_used == ["tone", "voice"]
