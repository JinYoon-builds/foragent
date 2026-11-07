"""Locked acceptance: skills and role prompts teach free text only through a quoted heredoc.

Incident (architect r768): role sessions copied `fa orch request --from X --text "<proposal>"` from
their skills and prompts; backtick spans inside the double quotes ran as shell commands.
test_free_text_from_stdin.py locks that these options accept `-` (read stdin).

Contract: no skill file (skills/** and the installed copies in .claude/skills/**) and no role prompt
(`build_prompt`, the public builder) shows a double-quoted value for the options below. Each command
that shows one of them with a value shows the stdin form with a QUOTED heredoc (`--text - <<'EOF'`),
and at most one `-` per command (one command has one stdin). Other texts of the same command may stay
single-quoted. Not pinned: the heredoc delimiter and the wording around the commands.
"""

from pathlib import Path
import re

import pytest

from foragent.orch_role_prompt import build_prompt

REPO = Path(__file__).resolve().parents[2]


# 2. No skill or role prompt teaches the double-quoted form.

FREE_TEXT = {  # command words -> its free-text options this issue moves to stdin
    ("orch", "request"): ("--text",),
    # --note stays out of this scan: the merged lock test_orch_order_handoff.py pins
    # `request-status ID done --note "<issues>"` (short issue numbers). It still accepts `-`.
    ("orch", "request-status"): ("--answer",),
    ("orch", "answer"): ("--text",),
    ("orch", "note"): ("--now", "--why", "--next", "--person-found"),
    ("orch", "role-decision"): ("--answer",),
    ("orch", "signal-answer"): ("--why",),
    ("inspect", "answer"): ("--text",),
    ("inspect", "feedback"): ("--body",),
    ("order", "note"): ("--text",),
    ("decisions", "add"): ("--text",),
    ("decisions", "drop"): ("--why",),
}
COMMAND = re.compile(r"\bfa\s+(\S+)\s+(\S+)([^`\n]*)")


def problems(name, text):
    """Commands that show a listed option with a value but not the quoted-heredoc stdin form."""
    found = []
    for number, line in enumerate(text.splitlines(), 1):
        for match in COMMAND.finditer(line):
            options = FREE_TEXT.get((match[1], match[2]))
            if not options:
                continue
            rest = match[3]
            valued = [option for option in options
                      if re.search(rf"(?<!\S){re.escape(option)}[ =](?!-\S)\S", rest)]
            quoted = [option for option in valued if re.search(rf"(?<!\S){re.escape(option)}[ =]\"", rest)]
            stdin = [option for option in options
                     if re.search(rf"(?<!\S){re.escape(option)}[ =]-(\s|$)", rest)]
            heredoc = any(re.search(rf"(?<!\S){re.escape(option)}[ =]-\s+<<-?\s*'", rest) for option in stdin)
            # One command has one stdin: a second `-` would read nothing.
            if quoted or len(stdin) > 1 or (valued and not heredoc):
                found.append(f"{name}:{number}: {match[0].strip()}")
    return found


def test_no_skill_teaches_a_double_quoted_free_text_value():
    # The project's skills and the installed copies role sessions read first (`.claude/skills`).
    files = sorted({path.resolve(): path for root in (REPO / "skills", REPO / ".claude/skills")
                    for path in root.rglob("*.md")}.values())
    assert files, "The skills folder must be readable from the test"
    found = [item for path in files
             for item in problems(path.relative_to(REPO).as_posix(), path.read_text(encoding="utf-8"))]
    assert not found, ("Teach free text through a quoted heredoc (`--text - <<'EOF'`), never in double "
                       "quotes:\n" + "\n".join(found))


@pytest.mark.parametrize("role", ["architect", "docs"])
@pytest.mark.parametrize("executor", ["claude", "codex"])
def test_role_prompts_teach_the_stdin_form(role, executor):
    prompt = build_prompt("Evidence: 65 % of unit-test time.", REPO, executor, role=role)
    assert "fa orch request" in prompt, "The role prompt must still say how to deliver a proposal"
    found = problems(f"{role} prompt ({executor})", prompt)
    assert not found, "Role prompts must teach `fa orch request --text - <<'EOF'`:\n" + "\n".join(found)
    assert re.search(r"fa orch request --from \S+ --text - <<-?\s*'", prompt), (
        "The proposal command must read its text from a quoted heredoc")
