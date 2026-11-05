"""#2110 regression: text read through `-` reaches every store exactly, edge whitespace included.

The acceptance file locks the round trip for prose; this file sends the whitespace a handler
would be tempted to tidy (leading tab, doubled spaces, inner and trailing blank lines) through
every listed option and reads the public record back. Only the heredoc's one newline may go.
"""

import io
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "acceptance"))

from foragent import cli  # noqa: E402
from test_orch_request_answer_record import request_case  # noqa: E402,F401 (fixture)
from test_orch_review_handoff import bind_cli, make_case  # noqa: E402
from test_role_signal_not_now import not_now_case, tick_signals  # noqa: E402,F401 (fixture)
from test_skills_autosync import cli_exit, isolated_home  # noqa: E402,F401 (fixture)
from test_tick_failure_alert import inbox, tick_case  # noqa: E402,F401 (fixture)

EDGE = "\tKeep  `fa stop`\n\n  indented\t$(x)\n\n"


def piped(monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO(EDGE + "\n"))


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


def test_request_text_keeps_edge_whitespace(request_case, monkeypatch, capsys):
    case = request_case
    piped(monkeypatch)
    ok(capsys, ["orch", "request", "--from", "architect", "--text", "-", "--repo-root", str(case.root)])
    [stored] = [item["text"] for item in box(case, capsys) if item["from"] == "architect"]
    assert stored == EDGE


@pytest.mark.parametrize("option", ["--answer", "--note"])
def test_request_status_keeps_edge_whitespace(request_case, monkeypatch, capsys, option):
    case = request_case
    request_id = new_request(case, capsys)
    other = ["--note", "Closed"] if option == "--answer" else ["--answer", "Prepared"]
    piped(monkeypatch)
    ok(capsys, ["orch", "request-status", request_id, "done", option, "-", *other, "--repo-root", str(case.root)])
    closed = row(case, capsys, request_id)
    stored = closed.get("answer") if option == "--answer" else [note["note"] for note in closed["notes"]][0]
    assert stored == EDGE


def test_role_decision_answer_keeps_edge_whitespace(request_case, monkeypatch, capsys):
    case = request_case
    request_id = new_request(case, capsys, "Consider a split", source="architect")
    piped(monkeypatch)
    ok(capsys, ["orch", "role-decision", request_id, "rejected", "--role", "architect", "--answer", "-",
                "--repo-root", str(case.root)])
    assert row(case, capsys, request_id).get("answer") == EDGE


def test_answer_text_keeps_edge_whitespace(request_case, monkeypatch, capsys):
    case = request_case
    root = ["--repo-root", str(case.root)]
    ok(capsys, ["orch", "ask", "--question", "Merge #41?", "--option", "Merge", "--option", "Later",
                "--recommend", "1", *root])
    [asked] = box(case, capsys, "questions")
    piped(monkeypatch)
    ok(capsys, ["orch", "answer", asked["id"], "--text", "-", *root])
    [closed] = [item for item in box(case, capsys, "questions", "--all") if item["id"] == asked["id"]]
    assert closed.get("answer") == EDGE


@pytest.mark.parametrize("option,key", [("--now", "now"), ("--why", "why"), ("--next", "next")])
def test_orchestrator_note_keeps_edge_whitespace(request_case, monkeypatch, capsys, option, key):
    from foragent import orch_inbox
    from foragent.state import StateStore

    case = request_case
    values = {"--now": "Reviewing issue 41", "--why": "Locale", "--next": "Answer"}
    values[option] = "-"
    piped(monkeypatch)
    ok(capsys, ["orch", "note", *[part for pair in values.items() for part in pair], "--repo-root", str(case.root)])
    assert (orch_inbox.read_note(StateStore(case.store.root)) or {}).get(key) == EDGE


def test_signal_answer_why_keeps_edge_whitespace(not_now_case, monkeypatch, capsys):  # noqa: F811
    case = not_now_case
    [raised] = tick_signals(case, capsys)
    piped(monkeypatch)
    ok(capsys, ["orch", "signal-answer", raised["id"], "not-now", "--why", "-", "--repo-root", str(case.root)])
    answered = {item["id"]: item for item in inbox(case, capsys)}[raised["id"]]
    [note] = [note["note"] for note in answered["notes"]]
    assert EDGE in note, repr(note)


def test_order_note_text_keeps_edge_whitespace(request_case, monkeypatch, capsys):
    case = request_case
    piped(monkeypatch)
    ok(capsys, ["order", "note", "--defer", "next-code-merge", "--kind", "decision", "--text", "-",
                "--repo-root", str(case.root)])
    shown = json.loads(ok(capsys, ["order", "pending", "--json", "--repo-root", str(case.root)]))
    [stored] = [note["text"] for note in shown["notes"] if note["kind"] == "decision"]
    assert stored == EDGE


def test_decision_drop_why_keeps_edge_whitespace(request_case, monkeypatch, capsys):
    case = request_case
    (case.root / "docs").mkdir(exist_ok=True)
    (case.root / "docs/decisions.md").write_text(
        "# Decisions\n\n### D9. Keep one gate (2026-09-01, Jin)\n- 결정: keep one gate.\n", encoding="utf-8")
    piped(monkeypatch)
    ok(capsys, ["decisions", "drop", "D9", "--why", "-", "--repo-root", str(case.root)])
    rows = json.loads(ok(capsys, ["decisions", "--json", "--repo-root", str(case.root)]))
    rows = rows if isinstance(rows, list) else rows.get("decisions", rows)
    [dropped] = [item for item in rows if item["id"] == "D9"]
    assert dropped.get("why") == EDGE


@pytest.mark.parametrize("action", ["answer", "feedback"])
def test_inspect_answer_and_feedback_keep_edge_whitespace(git_history, tmp_path, monkeypatch, capsys, action):
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
    assert EDGE in body, repr(body)


# The inline form keeps its old tidying; only text read through `-` is stored as written.

def feed(monkeypatch, text):
    monkeypatch.setattr(sys, "stdin", io.StringIO(text))


def inspect_case(git_history, tmp_path, monkeypatch):
    case = make_case(git_history, tmp_path, monkeypatch)
    bind_cli(case, monkeypatch)
    case.tracker.add_issue(41, "Decision for issue 41")
    return case


def decisions_of(case):
    return [comment for comment in case.tracker.list_comments(41) if "foragent:decision" in comment.body]


def test_inline_answer_still_reuses_but_stdin_text_is_never_folded_into_it(git_history, tmp_path, monkeypatch, capsys):
    case = inspect_case(git_history, tmp_path, monkeypatch)
    root = ["--repo-root", str(case.root)]
    ok(capsys, ["inspect", "answer", "41", "--text", "Keep", *root])
    ok(capsys, ["inspect", "answer", "41", "--text", " Keep ", *root])
    assert [comment.body.rsplit("\n", 1)[-1] for comment in decisions_of(case)] == ["Keep"], "Inline repeats reuse"
    feed(monkeypatch, "\tKeep\n\n\n")
    ok(capsys, ["inspect", "answer", "41", "--text", "-", *root])
    bodies = [comment.body for comment in decisions_of(case)]
    assert len(bodies) == 2 and bodies[-1].endswith("**Decision:**\n\tKeep\n\n"), bodies
    feed(monkeypatch, "\tKeep\n\n\n")
    ok(capsys, ["inspect", "answer", "41", "--text", "-", *root])
    assert len(decisions_of(case)) == 2, "The same stdin text again reuses its own exact comment"


def test_inline_feedback_still_reuses_one_issue_and_stdin_feedback_is_exact(git_history, tmp_path, monkeypatch, capsys):
    case = inspect_case(git_history, tmp_path, monkeypatch)
    root = ["--repo-root", str(case.root)]
    before = {issue.number for issue in case.tracker.list_issues()}
    for body in ("Spacing", " Spacing "):
        ok(capsys, ["inspect", "feedback", "--title", "Wrong spacing", "--body", body, *root])
    created = {issue.number for issue in case.tracker.list_issues()} - before
    assert len(created) == 1, "Inline bodies differing only at the edges are one feedback issue"
    for _ in range(2):
        feed(monkeypatch, "\tSpacing\n\n\n")
        ok(capsys, ["inspect", "feedback", "--title", "Wrong spacing", "--body", "-", *root])
    created = sorted({issue.number for issue in case.tracker.list_issues()} - before)
    assert len(created) == 2 and case.tracker.get_issue(created[-1]).body == "\tSpacing\n\n"


def test_inline_drop_reason_is_still_tidied(request_case, capsys):
    case = request_case
    (case.root / "docs").mkdir(exist_ok=True)
    (case.root / "docs/decisions.md").write_text(
        "# Decisions\n\n### D9. Keep one gate (2026-09-01, Jin)\n- 결정: keep one gate.\n", encoding="utf-8")
    ok(capsys, ["decisions", "drop", "D9", "--why", "  Superseded  ", "--repo-root", str(case.root)])
    rows = json.loads(ok(capsys, ["decisions", "--json", "--repo-root", str(case.root)]))
    rows = rows if isinstance(rows, list) else rows.get("decisions", rows)
    assert [item.get("why") for item in rows if item["id"] == "D9"] == ["Superseded"]


def test_inline_signal_reason_is_still_tidied(not_now_case, capsys):  # noqa: F811
    case = not_now_case
    [raised] = tick_signals(case, capsys)
    ok(capsys, ["orch", "signal-answer", raised["id"], "not-now", "--why", "  later  ", "--repo-root", str(case.root)])
    answered = {item["id"]: item for item in inbox(case, capsys)}[raised["id"]]
    assert [note["note"] for note in answered["notes"]] == ["role signal not-now: later"]
