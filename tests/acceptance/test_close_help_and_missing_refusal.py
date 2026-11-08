"""Acceptance tests for n85-close-help-and-refusal.

1. `fa orch close --help` exits 0 and explains the issue number, --superseded-by and --why,
   and says that closing superseded work closes its issue and PR, deletes the PR branch and
   removes its obsolete locked files from the base.
2. `fa orch close NUMBER --superseded-by OTHER --why REASON` for an issue that does not exist
   exits nonzero, names NUMBER, states that nothing changed, prints no traceback, and changes
   nothing: issue and PR states, branch refs, locked-file bytes, journal and queued writes.

Real CLI, real state store and a real local Git origin; the tracker is the project's fake
tracker, made to answer a missing issue the way GitHub does (HTTP 404 through gh).
"""

import re
from dataclasses import replace

import pytest

from conftest import git_command
from foragent import cli, main_write_requests, orch_events
from foragent.tracker import TrackerError
from test_orch_close_superseded import OWN, base_tip, superseded_case
from test_skills_autosync import cli_exit

MISSING = 99
WHY = "Its goal is now part of the merged base-refresh design."


# --- help ---------------------------------------------------------------------------------

def close_help(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)  # no project file: help must still render
    capsys.readouterr()
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["orch", "close", "--help"])
    return exit_info.value.code, capsys.readouterr().out


def entries(help_text):
    """argparse argument entries: invocation line plus deeper-indented continuation lines."""
    found, current = [], None
    for line in help_text.splitlines():
        if re.match(r"^  \S", line):
            current = [line.strip()]
            found.append(current)
        elif current is not None and re.match(r"^\s{3,}\S", line):
            current.append(line.strip())
        else:
            current = None
    return found


def explanation(help_text, pattern):
    """The help words given for the argument whose invocation starts with ``pattern``."""
    for entry in entries(help_text):
        head = entry[0]
        match = re.match(pattern, head, re.I)
        if match is None:
            continue
        invocation = re.match(r"^(\S+(?: [A-Z_\[\].]+(?: \.\.\.\])?)*)", head)
        rest = head[len(invocation[1]):] if invocation else head[match.end():]
        return " ".join([rest.strip(), *entry[1:]]).strip()
    return None


def test_close_help_explains_arguments_and_effects(tmp_path, monkeypatch, capsys):
    code, out = close_help(tmp_path, monkeypatch, capsys)
    assert code == 0, f"fa orch close --help must exit 0: {out}"
    flat = " ".join(out.split())
    for pattern, name in ((r"number\b", "number"), (r"--superseded-by\b", "--superseded-by"),
                          (r"--why\b", "--why")):
        words = explanation(out, pattern)
        assert words is not None, f"The help must list {name}: {out}"
        assert re.search(r"[A-Za-z가-힣]{3,}", words or ""), (
            f"The help must explain {name}, not only name it: {out}"
        )
    usage_and_args = re.split(r"\n(?=positional arguments|options|optional arguments)", out, maxsplit=1)
    description = usage_and_args[0] if usage_and_args else out
    lower = flat.lower()
    assert re.search(r"\bpr\b|pull request", lower), f"The help must say the PR closes: {out}"
    assert "branch" in lower and re.search(r"delet", lower), (
        f"The help must say the PR branch is deleted: {out}"
    )
    assert re.search(r"lock", lower) and re.search(r"\bbase\b", lower), (
        f"The help must say obsolete locked files leave the base: {out}"
    )
    assert re.search(r"\bissue\b", lower) and re.search(r"clos", lower), (
        f"The help must say the issue closes: {out}"
    )
    assert re.search(r"\b(clos|delet|remov)", description.lower()), (
        f"The command description itself must state the closure's effects: {description}"
    )


# --- missing target -----------------------------------------------------------------------

def missing_case(git_history, tmp_path, monkeypatch, language="en"):
    """The superseded-work world, plus a tracker that answers a missing issue like gh (HTTP 404)."""
    case = superseded_case(git_history, tmp_path, monkeypatch)
    if language != "en":
        set_language(case, language)
    original_get, original_comments = case.tracker.get_issue, case.tracker.list_comments

    def get_issue(n):
        if n not in case.tracker.issues:
            raise TrackerError("gh: Not Found (HTTP 404)", returncode=1)
        return original_get(n)

    def list_comments(n):
        if n not in case.tracker.issues:
            raise TrackerError("gh: Not Found (HTTP 404)", returncode=1)
        return original_comments(n)

    monkeypatch.setattr(case.tracker, "get_issue", get_issue)
    monkeypatch.setattr(case.tracker, "list_comments", list_comments)
    return case


def set_language(case, language):
    config = replace(case.ctx.config, language=language)
    try:
        case.ctx.config = config
    except Exception:
        object.__setattr__(case.ctx, "config", config)


def snapshot(case):
    tracker = case.tracker
    return {
        "issues": {n: (issue.state, tuple(sorted(issue.labels)), len(tracker.list_comments(n)))
                   for n, issue in sorted(tracker.issues.items())},
        "deleted": list(tracker.deleted_branches),
        "refs": git_command(case.history.origin, "for-each-ref", "--format=%(refname) %(objectname)"),
        "tip": base_tip(case),
        "lock": git_command(case.history.origin, "show", f"refs/heads/main:{OWN}"),
        "events": orch_events.read_events(case.store),
        "main_writes": main_write_requests.requests(case.store),
        "outbox": case.store.github_outbox(),
        "history": [(run.number, run.outcome) for run in case.store.read_history()],
    }


def refuse_missing(case, capsys):
    before = snapshot(case)
    capsys.readouterr()
    code = cli_exit(case.main, ["orch", "close", str(MISSING), "--superseded-by", "2", "--why", WHY])
    output = capsys.readouterr()
    return code, output.out + output.err, before


def test_close_refuses_a_missing_issue_by_number_and_changes_nothing(git_history, tmp_path, monkeypatch, capsys):
    case = missing_case(git_history, tmp_path, monkeypatch)

    code, text, before = refuse_missing(case, capsys)

    assert code not in (0, None), f"Closing a missing issue must fail: {text}"
    assert "Traceback" not in text, f"The refusal must not print a traceback: {text}"
    assert re.search(rf"(#|issue\s+){MISSING}\b", text, re.I), (
        f"The refusal must name issue {MISSING}: {text}"
    )
    assert re.search(r"nothing (was )?changed|nothing has changed|no changes? (were|was )?made", text, re.I), (
        f"The refusal must state plainly that nothing changed: {text}"
    )
    assert snapshot(case) == before, "A refused close must not change issues, PRs, refs, locks or records"
    assert case.tracker.get_issue(case.pr).state == "open"
    assert case.tracker.deleted_branches == []


def test_close_refuses_a_missing_issue_in_korean(git_history, tmp_path, monkeypatch, capsys):
    case = missing_case(git_history, tmp_path, monkeypatch, language="ko")

    code, text, before = refuse_missing(case, capsys)

    assert code not in (0, None), f"Closing a missing issue must fail: {text}"
    assert "Traceback" not in text, f"The refusal must not print a traceback: {text}"
    assert re.search(rf"#{MISSING}\b|{MISSING}\s*번", text), f"The refusal must name issue {MISSING}: {text}"
    assert re.search(r"아무것도\s*(바꾸지|변경하지|바뀌지)", text), (
        f"The Korean refusal must state that nothing changed: {text}"
    )
    assert snapshot(case) == before, "A refused close must not change issues, PRs, refs, locks or records"
