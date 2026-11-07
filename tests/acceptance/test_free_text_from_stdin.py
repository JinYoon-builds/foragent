"""Locked acceptance: free text reaches role-facing commands through stdin, never through the shell.

Incident (architect r768, 2026-11-03..05): role sessions passed prose as
`fa orch request --from X --text "..."`. Backtick spans inside the double quotes ran as shell
command substitution: six accidental commands in two days (one `fa stop` paused every lane of the
live project), and every one corrupted the stored text.

Contract declared by this issue:
- Each free-text option below accepts the single value `-`, meaning "read the text from stdin".
  The text read is stored exactly (backticks, `$(...)`, both quote kinds, tabs and newlines kept);
  only the trailing newline a heredoc adds may be dropped. The old `--option "text"` form keeps working.
- The templates that teach these commands are locked in test_free_text_templates.py.
Not pinned: how stdin is read, which module does it, the heredoc delimiter, and the wording around
the commands. Every reader below is a public reader (`--json` listings, the GitHub issue fake).
"""

import io
import json
from pathlib import Path
import subprocess
import sys

import pytest

from foragent import cli
from test_orch_request_answer_record import request_case  # noqa: F401 (fixture)
from test_orch_review_handoff import bind_cli, make_case
from test_role_signal_not_now import not_now_case, tick_signals  # noqa: F401 (fixture)
from test_skills_autosync import cli_exit, isolated_home  # noqa: F401 (fixture)
from test_tick_failure_alert import inbox, tick_case  # noqa: F401 (fixture)

# Everything that broke the live text: backtick spans, $(...), both quote kinds, tab, backslash, $VAR.
TEXT = ("Proposal: split `orch_inbox.py`; do not run `fa stop` or $(gh pr view 1).\n"
        "He said \"keep it\" and 'it's fine'.\n"
        "\tEvidence: C:\\tmp \\n stays literal, $HOME stays literal, 65 % of test time.")
REPO = Path(__file__).resolve().parents[2]


def same(stored):
    assert isinstance(stored, str), f"The text must be stored as text, got {stored!r}"
    assert stored.rstrip("\n") == TEXT, (
        f"`-` must store exactly the stdin text.\nstored:   {stored!r}\nexpected: {TEXT!r}")


def piped(monkeypatch):
    """What `--option - <<'EOF' ... EOF` hands the process: the text plus the heredoc's newline."""
    monkeypatch.setattr(sys, "stdin", io.StringIO(TEXT + "\n"))


def ok(capsys, argv):
    capsys.readouterr()
    code = cli_exit(cli.main, argv)
    output = capsys.readouterr()
    assert code == 0, f"fa {' '.join(argv)} failed: {output.err.strip()}"
    return output.out


def box(case, capsys, kind="inbox", *extra):
    return json.loads(ok(capsys, ["orch", kind, "--json", *extra, "--repo-root", str(case.root)]))


def row(case, capsys, request_id):
    return {item["id"]: item for item in box(case, capsys)}[request_id]


def new_request(case, capsys, text="Prepare the requested order", source="planning"):
    ok(capsys, ["orch", "request", "--from", source, "--text", text, "--repo-root", str(case.root)])
    return next(item["id"] for item in box(case, capsys) if item["text"] == text)


# 1. Every listed free-text option stores the stdin text exactly.

def test_request_text_from_stdin_and_the_old_form_still_works(request_case, monkeypatch, capsys):
    case = request_case
    plain = new_request(case, capsys, "A plain one-line request")
    piped(monkeypatch)
    ok(capsys, ["orch", "request", "--from", "architect", "--text", "-", "--repo-root", str(case.root)])
    rows = [item for item in box(case, capsys) if item["from"] == "architect"]
    assert len(rows) == 1, f"One request per call: {rows}"
    same(rows[0]["text"])
    assert row(case, capsys, plain)["text"] == "A plain one-line request", "The old --text \"x\" form keeps working"


@pytest.mark.parametrize("option", ["--answer", "--note"])
def test_request_status_answer_and_note_from_stdin(request_case, monkeypatch, capsys, option):
    case = request_case
    request_id = new_request(case, capsys)
    other = ["--note", "Closed"] if option == "--answer" else ["--answer", "Prepared two issues"]
    piped(monkeypatch)
    ok(capsys, ["orch", "request-status", request_id, "done", option, "-", *other, "--repo-root", str(case.root)])
    closed = row(case, capsys, request_id)
    assert closed["status"] == "done"
    if option == "--answer":
        same(closed.get("answer"))
    else:
        notes = [note["note"] for note in closed.get("notes", [])]
        assert len(notes) == 1, notes
        same(notes[0])


def test_role_decision_answer_from_stdin(request_case, monkeypatch, capsys):
    case = request_case
    request_id = new_request(case, capsys, "Consider splitting the shared module", source="architect")
    piped(monkeypatch)
    ok(capsys, ["orch", "role-decision", request_id, "rejected", "--role", "architect", "--answer", "-",
                "--repo-root", str(case.root)])
    decided = row(case, capsys, request_id)
    assert decided["status"] == "done" and decided.get("accepted") is False
    same(decided.get("answer"))


def test_answer_text_from_stdin(request_case, monkeypatch, capsys):
    case = request_case
    root = ["--repo-root", str(case.root)]
    ok(capsys, ["orch", "ask", "--question", "Merge #41?", "--option", "Merge", "--option", "Later",
                "--recommend", "1", *root])
    [asked] = box(case, capsys, "questions")
    piped(monkeypatch)
    ok(capsys, ["orch", "answer", asked["id"], "--text", "-", *root])
    [closed] = [item for item in box(case, capsys, "questions", "--all") if item["id"] == asked["id"]]
    same(closed.get("answer"))


@pytest.mark.parametrize("option", ["--now", "--why", "--next"])
def test_orchestrator_note_from_stdin(request_case, monkeypatch, capsys, option):
    from foragent import orch_inbox
    from foragent.state import StateStore

    case = request_case
    values = {"--now": "Reviewing issue 41", "--why": "The locale contract changed", "--next": "Answer the request"}
    values[option] = "-"
    piped(monkeypatch)
    ok(capsys, ["orch", "note", *[part for pair in values.items() for part in pair], "--repo-root", str(case.root)])
    note = orch_inbox.read_note(StateStore(case.store.root)) or {}
    key = {"--now": "now", "--why": "why", "--next": "next"}[option]
    same(note.get(key))
    for other, value in values.items():
        if other != option:
            assert note.get({"--now": "now", "--why": "why", "--next": "next"}[other]) == value


def test_signal_answer_why_from_stdin(not_now_case, monkeypatch, capsys):  # noqa: F811
    case = not_now_case
    [raised] = tick_signals(case, capsys)
    piped(monkeypatch)
    ok(capsys, ["orch", "signal-answer", raised["id"], "not-now", "--why", "-", "--repo-root", str(case.root)])
    answered = {item["id"]: item for item in inbox(case, capsys)}[raised["id"]]
    assert answered["status"] == "done"
    notes = [note["note"] for note in answered.get("notes", [])]
    assert len(notes) == 1 and TEXT in notes[0], f"The reason must be kept exactly: {notes!r}"


def test_order_note_text_from_stdin(request_case, monkeypatch, capsys):
    case = request_case
    piped(monkeypatch)
    ok(capsys, ["order", "note", "--defer", "next-code-merge", "--kind", "decision", "--text", "-",
                "--repo-root", str(case.root)])
    shown = json.loads(ok(capsys, ["order", "pending", "--json", "--repo-root", str(case.root)]))
    notes = [note for note in shown["notes"] if note["kind"] == "decision"]
    assert len(notes) == 1, notes
    same(notes[0]["text"])


def test_decision_drop_why_from_stdin(request_case, monkeypatch, capsys):
    case = request_case
    (case.root / "docs").mkdir(exist_ok=True)
    (case.root / "docs/decisions.md").write_text(
        "# Decisions\n\n### D9. Keep one gate (2026-09-01, Jin)\n- 결정: keep one gate.\n", encoding="utf-8")
    piped(monkeypatch)
    ok(capsys, ["decisions", "drop", "D9", "--why", "-", "--repo-root", str(case.root)])
    rows = json.loads(ok(capsys, ["decisions", "--json", "--repo-root", str(case.root)]))
    rows = rows if isinstance(rows, list) else rows.get("decisions", rows)
    [dropped] = [item for item in rows if item["id"] == "D9"]
    same(dropped.get("why"))


@pytest.mark.parametrize("action", ["answer", "feedback"])
def test_inspect_answer_and_feedback_from_stdin(git_history, tmp_path, monkeypatch, capsys, action):
    case = make_case(git_history, tmp_path, monkeypatch)
    bind_cli(case, monkeypatch)
    case.tracker.add_issue(41, "Decision for issue 41")
    before = {issue.number for issue in case.tracker.list_issues()}
    piped(monkeypatch)
    if action == "answer":
        ok(capsys, ["inspect", "answer", "41", "--text", "-", "--repo-root", str(case.root)])
        body = case.tracker.list_comments(41)[-1].body
    else:
        ok(capsys, ["inspect", "feedback", "--title", "Wrong spacing", "--body", "-", "--about", "41",
                    "--repo-root", str(case.root)])
        [number] = {issue.number for issue in case.tracker.list_issues()} - before
        body = case.tracker.get_issue(number).body
    assert TEXT in body, f"The GitHub text must carry the stdin text exactly:\n{body}"


def test_a_real_shell_heredoc_runs_none_of_the_text(tmp_path):
    """End to end through /bin/sh: the documented form runs no backtick span and keeps the text."""
    home, project = tmp_path / "shell-home", tmp_path / "shell-project"
    project.mkdir(parents=True)
    home.mkdir()
    (project / ".foragent.yml").write_text("repo: owner/repo\nskill_libraries: []\nnotify: none\n", encoding="utf-8")
    marker = tmp_path / "ran"
    text = f"Do not run `touch {marker}` or $(touch {marker}).\nKeep \"quotes\" and 'these'."
    environment = {"HOME": str(home), "PATH": "/usr/bin:/bin", "XDG_CONFIG_HOME": str(home / ".config"),
                   "XDG_STATE_HOME": str(home / ".local/state"), "GIT_CONFIG_NOSYSTEM": "1",
                   "PYTHONPATH": str(REPO / "src")}
    fa = f"{sys.executable} -c 'import sys; from foragent.cli import main; sys.exit(main())'"
    script = (f"{fa} orch request --from architect --text - --repo-root {project} <<'EOF'\n{text}\nEOF\n"
              f"{fa} orch inbox --json --repo-root {project}\n")
    done = subprocess.run(["/bin/sh", "-c", script], cwd=project, env=environment, capture_output=True,
                          text=True, timeout=120)
    assert not marker.exists(), "A quoted heredoc must never run a span of the text"
    assert done.returncode == 0, done.stderr
    rows = json.loads(done.stdout.strip().splitlines()[-1])
    assert [item["text"].rstrip("\n") for item in rows] == [text], rows
