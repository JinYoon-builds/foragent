"""A timeout escalation is stalled work, never a question for the person."""

from dataclasses import asdict
from datetime import timedelta

import pytest

from foragent.config import load_config
from foragent.tracker import FakeTracker
from test_fa_short_report import short  # noqa: F401
from test_skills_autosync import isolated_home  # noqa: F401


BLOCKERS = [
    (501, "Wait for CI", "timeout", "CI did not finish"),
    (502, "Resolve review disagreement", "review-dispute", "Review findings need an orchestrator look."),
    (503, "Recover stalled implementation", "stalled", "Implementation stopped making progress."),
]


@pytest.fixture(params=["en", "ko"])
def person_question_report(short, request):
    case = short
    language = request.param
    case.configure(language=language)
    tracker = FakeTracker(now=lambda: case.clock[0])

    case.clock[0] -= timedelta(minutes=31)
    for number, title, reason, details in BLOCKERS:
        tracker.add_issue(number, title, labels=["blocked", "lane:api"])
        tracker.comment(number, f"<!-- foragent:blocked reason={reason} -->\n{details}")
    case.clock[0] += timedelta(minutes=31)
    for number, _, _, _ in BLOCKERS:
        blocker = tracker.list_comments(number)[-1]
        tracker.add_labels(number, ["needs-human"])
        tracker.comment(number,
                        "<!-- foragent:needs-human reason=orchestrator-timeout -->\n"
                        "Orchestrator timeout: no response to this blocker within 30 minutes.\n\n"
                        f"Latest blocker:\n{blocker.body}")

    # Import comments as well as labels: the report must have the actual blocker
    # history available locally, just as it does after a gate refresh.
    case.store.refresh_local_issues(
        tracker.list_issues("all"), [], load_config(case.root).lanes, case.clock[0],
        comments={number: [asdict(comment) for comment in tracker.list_comments(number)]
                  for number, _, _, _ in BLOCKERS},
    )
    # The fixture calls orch_inbox.ask, the same API used by `fa orch ask`.
    question = case.ask("Which storage region should we use?", ["Seoul", "Tokyo"], 1, 401)
    return case, language, question


def test_short_report_counts_only_the_open_person_question(person_question_report):
    case, language, _ = person_question_report
    output = case.brief("--report")
    first = output.splitlines()[0]
    expected = "1 waiting for you" if language == "en" else "결정 기다리는 일 1개"
    assert expected in first.split(" · "), (
        "Only the PM-forwarded question waits for the person; the three "
        f"orchestrator timeouts are not decisions.\nReport:\n{output}"
    )


def test_full_report_lists_the_question_and_options_without_timeout_issues(person_question_report):
    case, language, question = person_question_report
    output = case.brief("--report", "--full")
    heading = "Your turn:" if language == "en" else "정해 주실 일:"
    lines = output.splitlines()
    assert heading in lines, f"The report must have a decisions section:\n{output}"
    decisions = []
    for line in lines[lines.index(heading) + 1:]:
        if line.strip() and not line.startswith(("- ", " ", "\t")):
            break
        decisions.append(line)
    decision_list = "\n".join(decisions)
    assert question["question"] in decision_list, output
    for option in question["options"]:
        assert option in decision_list, f"Show every option with the person's question:\n{output}"
    for number, title, _, details in BLOCKERS:
        assert f"#{number}" not in decision_list and title not in decision_list, (
            f"Timeout-only issue #{number} must not appear under {heading}\nReport:\n{output}"
        )
        if f"#{number}" in output:
            assert details in output, f"Stalled work must retain its underlying blocker:\n{output}"
