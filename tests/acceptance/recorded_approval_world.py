"""Shared world for the recorded-approval acceptance tests (issue approval-record).

Public entry points only: ``fa order approval PLAN.json --words TEXT`` records the
person's first-hand approval of the plan's scope (plan id, each issue key with its goal,
the words, the time), and ``fa order publish PLAN.json --approve --recorded-approval``
publishes with that record. Git, tracker and executor use the existing offline
publication world (``tests/test_order.py::publishing``).
"""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import re
import socket
import time
from types import SimpleNamespace

import pytest

from test_local_truth import record_gh
from test_publish_out_of_scope import publish_cli
from test_publish_own_paths import with_clean_preread
from test_skills_autosync import cli_exit


WORDS = "좋아요, 리포트와 내보내기 둘 다 이대로 진행해요 (r1265)"
ALPHA, BETA, GAMMA = "alpha-report", "beta-export", "gamma-extra"


def item(key, title, goal):
    return {"key": key, "title": title, "goal": goal, "out_of_scope": "Other work.",
            "done_when": ["true"], "lane": "api", "size": "s", "acceptance_files": {}}


def plan_value():
    return {
        "id": "2026-11-08-recorded-approval-check", "title": "Recorded approval check",
        "summary": "The orchestrator drafted this plan; the person approved it in planning.",
        "spec_markdown": "# Recorded approval\n",
        "issues": [
            item(ALPHA, "Build the weekly report", "Send a weekly usage report to each owner."),
            item(BETA, "Export the report as CSV", "Let owners download the weekly report as CSV."),
        ],
    }


def make_case(publishing, monkeypatch):
    root, config, tracker, git, events = publishing
    config = replace(config, language="en", executor="codex", skill_libraries=[])
    monkeypatch.chdir(root)
    calls = record_gh(monkeypatch)

    def no_network(*args, **kwargs):
        pytest.fail("Recorded-approval publication must use the offline tracker")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    main = with_clean_preread(publish_cli((root, config, tracker, git, events), monkeypatch), monkeypatch)
    return SimpleNamespace(root=root, tracker=tracker, events=events, plan=plan_value(),
                           path=root / "PLAN.json", main=main, gh=calls)


def run(case, capsys, argv):
    capsys.readouterr()
    code = cli_exit(case.main, [*argv, "--repo-root", str(case.root)])
    captured = capsys.readouterr()
    assert case.gh == [], "No GitHub process may run in these acceptance tests"
    return code, captured.out + captured.err


def write_plan(case):
    case.path.write_text(json.dumps(case.plan), encoding="utf-8")


def record(case, capsys, words=WORDS):
    """Planning records the person's approval of the plan as it stands now.

    Returns the UTC window, at whole seconds, in which the record was made."""
    write_plan(case)
    before = datetime.now(timezone.utc).replace(microsecond=0)
    code, output = run(case, capsys, ["order", "approval", str(case.path), "--words", words])
    after = datetime.now(timezone.utc).replace(microsecond=0)
    assert code == 0, f"Planning must be able to record the person's approval: {output}"
    # Publishing happens visibly later than the record, so a shown publish time
    # cannot pass for the recorded approval time.
    time.sleep(2.2)
    return before, after


def publish(case, capsys, *options):
    write_plan(case)
    return run(case, capsys, ["order", "publish", str(case.path), *options])


def child(case, title):
    matches = [issue for issue in case.tracker.list_issues() if issue.title == title]
    assert len(matches) == 1, f"Expected one published issue titled {title!r}: {matches}"
    return matches[0]


def evidence_text(case, issue):
    comments = case.tracker.list_comments(issue.number)
    return "\n".join([issue.body or "", *(comment.body for comment in comments)])


_STAMP = re.compile(
    r"(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})(?:\.\d+)?\s*(Z|[+-]\d{2}:?\d{2}|UTC)?")


def stamps(text):
    """Every second-precision timestamp in the text, as UTC candidates."""
    found = []
    for day, clock, zone in _STAMP.findall(text):
        moment = datetime.fromisoformat(f"{day}T{clock}")
        if zone in ("", "Z", "UTC"):
            found.append(moment.replace(tzinfo=timezone.utc))
        else:
            sign = 1 if zone[0] == "+" else -1
            hours, minutes = int(zone[1:3]), int(zone[-2:])
            found.append(moment.replace(tzinfo=timezone(sign * timedelta(hours=hours, minutes=minutes))))
        if zone == "":  # A naive time may also be the machine's local time.
            found.append(moment.astimezone(timezone.utc))
    return found


def assert_recorded_evidence(case, issue, window, words=WORDS):
    text = evidence_text(case, issue)
    assert text.count(words) == 1, (
        f"#{issue.number} must show the person's recorded words once as approval evidence:\n{text}")
    before, after = window
    shown = stamps(text)
    assert any(before <= moment <= after for moment in shown), (
        f"#{issue.number} must show when the approval was recorded ({before}..{after}); shown: {shown}\n{text}")
