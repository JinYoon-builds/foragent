"""Acceptance test for person-count-real.

The dashboard line "답할 질문이 N개 있어요" ("N questions to answer") must count only
items still waiting on the person. On 2026-11-07 09:05 the screen said 6 while the
real count was 0: needs-human issues whose decision was already recorded with
`fa inspect answer` (now waiting for the PM's fix or retry), and PRs already in the
merge-owner queue or a merge batch, were counted and listed as the person's.

Public entry points only: `fa inspect answer`, `fa orch ask`, `fa gate watch --once`
(the dashboard) and `fa statusline`. GitHub is the in-memory fake tracker, observed
into local records the way the gate's refresh does. PRs in the merge-owner queue or a
merge batch are covered in test_person_count_merge.py.

Reading the screen does not pin a layout:
- the person's count is every "N question(s) to answer" / "답할 질문(이) N개" phrase on
  the dashboard (header and bubble), and no such phrase means zero;
- an item listed as the person's is an acorn whose word is a question
  ("#N question…" / "#N 질문…").
"""

from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from io import BytesIO
import re
import sys
from types import SimpleNamespace

import pytest
import yaml

from foragent.config import load_config
from foragent.state import StateStore
from foragent.tracker import FakeTracker
from test_order_progress import offline_local_cli
from test_skills_autosync import cli_exit, isolated_home  # noqa: F401
from test_squirrel_running import plain_frame, view_offline  # noqa: F401

PERSON = {"ko": ("진님",), "en": ("you",)}
ELSEWHERE = {"ko": r"오케스트레이터|\bPM\b|기획", "en": r"orchestrator|\bPM\b|planning"}
QUESTION_WORD = {"ko": "질문", "en": "question"}


# ---------------------------------------------------------------- screen reading

def person_counts(frame, language):
    """Every count of the person's questions the dashboard prints (header, bubble)."""
    pattern = r"답할 질문이? ?(\d+)\s*개" if language == "ko" else r"(\d+) questions? to answer"
    counts = [int(value) for value in re.findall(pattern, frame)]
    if language == "en":
        counts += [1 for _ in re.findall(r"\bone question to answer", frame, re.I)]
    return counts


def acorns(frame):
    """(number, its words) for every acorn drawn: pixel '◖█#N word▌' or ASCII '[#N word]'."""
    found = []
    for line in plain_frame(frame).splitlines():
        if "▌" not in line and "[" not in line:
            continue
        for match in re.finditer(r"#(\d+)([^▌\]#]*)", line):
            found.append((int(match[1]), match[2].strip()))
    return found


def asked_of_person(frame, language):
    """Issue numbers the dashboard lists as the person's question."""
    word = QUESTION_WORD[language]
    return sorted({number for number, words in acorns(frame) if word in words.casefold()})


def acorn_words(frame, number):
    words = [text for n, text in acorns(frame) if n == number]
    assert words, f"The dashboard must still show an acorn for #{number}:\n{frame}"
    return " ".join(words)


def names_person(text, language):
    folded = text.casefold()
    return any(re.search(rf"(?<!\w){re.escape(name)}(?!\w)", folded) for name in PERSON[language])


# ---------------------------------------------------------------- offline project

@pytest.fixture(params=["ko", "en"])
def count_case(request, tmp_path, monkeypatch, view_offline):  # noqa: F811
    from foragent import cli, statusline

    case = offline_local_cli(tmp_path, monkeypatch)
    case.language = request.param
    (case.root / ".foragent.yml").write_text(yaml.safe_dump({
        "repo": "owner/repo", "language": case.language, "skill_libraries": [], "notify": "none",
        "lanes": {"work": {"paths": ["work/**"]}},
    }), encoding="utf-8")
    case.config = load_config(case.root)
    # Close to the real clock and before it: `fa inspect answer` and `fa orch ask` stamp the
    # real time, so every answer comes after the routing it answers.
    case.clock = [(datetime.now(UTC) - timedelta(hours=3)).replace(microsecond=0)]
    case.store = StateStore(case.store.root, now=lambda: case.clock[0])
    case.tracker = FakeTracker(now=lambda: case.clock[0])
    monkeypatch.setattr(cli, "GhTracker", lambda repo: case.tracker)
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setenv("LANG", "en_US.UTF-8")
    monkeypatch.setenv("COLUMNS", "200")
    monkeypatch.delenv("LC_ALL", raising=False)
    monkeypatch.delenv(statusline.NESTED_ENV, raising=False)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return case.clock[0].astimezone(tz) if tz else case.clock[0].replace(tzinfo=None)

    monkeypatch.setattr(statusline, "datetime", Clock)
    return case


def route_to_person(case, number, reason="orchestrator-timeout"):
    """A held issue the gate routed to the person, as GitHub shows it (label + marker comment)."""
    case.tracker.add_issue(number, f"Held work {number}",
                           labels=["approved", "blocked", "needs-human", "lane:work"])
    case.store.start_run(number, branch=f"foragent/issue-{number}",
                         worktree=case.store.worktree_path(number), lane="work")
    case.store.finish_run(number, outcome="held", detail="Held for a decision.")
    case.clock[0] += timedelta(minutes=1)
    case.tracker.comment(number, f"<!-- foragent:needs-human reason={reason} -->\n"
                                 f"**Needs a decision for #{number}**\n\nChoose how to continue.")
    case.clock[0] += timedelta(minutes=1)


def record_decision(case, number):
    """The person's answer, recorded with the public command."""
    assert cli_exit(case.main, ["inspect", "answer", str(number), "--text", "Retry with the smaller scope.",
                                "--repo-root", str(case.root)]) == 0
    decided = [comment for comment in case.tracker.list_comments(number)
               if comment.body.startswith("<!-- foragent:decision -->")]
    assert decided, "Setup: `fa inspect answer` records the decision on the issue"


def observe(case):
    """The gate's local observation of GitHub: issues, labels and their comments."""
    issues = case.tracker.list_issues("all")
    comments = {issue.number: [asdict(comment) for comment in case.tracker.list_comments(issue.number)]
                for issue in issues}
    case.store.refresh_local_issues(issues, [], case.config.lanes, case.clock[0], comments=comments)


def dashboard(case, capsys):
    capsys.readouterr()
    assert cli_exit(case.main, ["gate", "watch", "--once", "--repo-root", str(case.root)]) == 0
    return plain_frame(capsys.readouterr().out)


def statusline_text(case, monkeypatch):
    output = BytesIO()
    with monkeypatch.context() as streams:
        streams.setattr(sys, "stdin", SimpleNamespace(buffer=BytesIO(b"{}\n")))
        streams.setattr(sys, "stdout", SimpleNamespace(buffer=output))
        assert cli_exit(case.main, ["statusline"]) == 0
    return plain_frame(output.getvalue().decode("utf-8"))


# ---------------------------------------------------------------- statement 1

def test_answered_needs_human_issue_is_not_the_persons_and_waits_on_the_pm_or_orchestrator(
        count_case, monkeypatch, capsys):
    """Reject counting or listing an issue whose decision `fa inspect answer` already recorded."""
    case = count_case
    route_to_person(case, 2176)   # answered: waits for the PM's fix or retry
    route_to_person(case, 2177)   # still the person's
    record_decision(case, 2176)
    observe(case)
    frame = dashboard(case, capsys)

    assert asked_of_person(frame, case.language) == [2177], (
        f"Only the unanswered issue is listed as the person's question:\n{frame}")
    assert person_counts(frame, case.language) and set(person_counts(frame, case.language)) == {1}, (
        f"The person's count is 1: the answered issue is not counted:\n{frame}")
    words = acorn_words(frame, 2176)
    assert not names_person(words, case.language), f"#2176 must not name the person: {words!r}"
    assert re.search(ELSEWHERE[case.language], words, re.I), (
        f"#2176 shows as waiting on the PM or orchestrator, not the person: {words!r}")

    line = statusline_text(case, monkeypatch)
    numbers = [int(value) for value in re.findall(r"\d+", line)]
    assert 2176 not in numbers, f"The statusline must not list the answered issue: {line}"


def test_only_answered_items_leave_the_count(count_case, monkeypatch, capsys):
    """Reject a count that still includes the answered issue once every other item is answered."""
    case = count_case
    route_to_person(case, 2340)
    route_to_person(case, 2352, reason="no-machine-check")
    record_decision(case, 2340)
    record_decision(case, 2352)
    observe(case)
    frame = dashboard(case, capsys)
    assert person_counts(frame, case.language) == [], (
        f"Nothing waits on the person, so no question count is shown:\n{frame}")
    assert asked_of_person(frame, case.language) == [], frame
    line = statusline_text(case, monkeypatch)
    assert not names_person(line, case.language), f"The statusline must not name the person: {line}"
