"""`fa brief --report --full` shows each stalled item's whole reason in plain text (r1022)."""

from dataclasses import asdict
from datetime import timedelta

import pytest

from foragent.config import load_config
from foragent.i18n import text
from foragent.tracker import FakeTracker
from test_fa_short_report import short  # noqa: F401
from test_skills_autosync import isolated_home  # noqa: F401


NUMBER = 2458
TITLE = "fa orch retry refuses a third try of the same defect class"
BOLD = "Two failed rounds of the same defect class"
REST = (
    "The second review rejected the retry for the very same reason as the first one: "
    "the runner records the CI result only on the success path, so a failed head leaves "
    "no fact behind and the next round guesses again. A third automatic try would repeat "
    "the guess, so the orchestrator must decide whether to split the work or narrow the scope."
)
BLOCKER_BODY = f"**{BOLD}**\n\n{REST}"
PLAIN = f"{BOLD} {REST}"
HEADINGS = {"en": "Stalled, no answer from the orchestrator:", "ko": "멈춘 일 (오케스트레이터 응답 없음):"}


def _words(text):
    return " ".join(text.split())


@pytest.fixture(params=["en", "ko"])
def stalled_report(short, request):  # noqa: F811
    case = short
    language = request.param
    case.configure(language=language)
    tracker = FakeTracker(now=lambda: case.clock[0])

    case.clock[0] -= timedelta(minutes=31)
    tracker.add_issue(NUMBER, TITLE, labels=["blocked", "lane:api"])
    tracker.comment(NUMBER, f"<!-- foragent:blocked reason=retries-exceeded -->\n{BLOCKER_BODY}")
    case.clock[0] += timedelta(minutes=31)
    blocker = tracker.list_comments(NUMBER)[-1]
    tracker.add_labels(NUMBER, ["needs-human"])
    tracker.comment(NUMBER,
                    "<!-- foragent:needs-human reason=orchestrator-timeout -->\n"
                    "Orchestrator timeout: no response to this blocker within 30 minutes.\n\n"
                    f"Latest blocker:\n{blocker.body}")
    case.store.refresh_local_issues(
        tracker.list_issues("all"), [], load_config(case.root).lanes, case.clock[0],
        comments={NUMBER: [asdict(comment) for comment in tracker.list_comments(NUMBER)]},
    )
    return case, language


def _stalled_entry(output, language):
    lines = output.splitlines()
    heading = HEADINGS[language]
    assert heading in lines, f"The full report must list the stalled issue under {heading!r}:\n{output}"
    after = {text("brief_report_your_turn", language), text("brief_report_nothing_needed", language)}
    section = []
    for line in lines[lines.index(heading) + 1:]:
        if not line.strip() or line.strip() in after or (
                not line.startswith((" ", "\t", "-")) and line.rstrip().endswith(":")):
            break
        section.append(line)
    starts = [index for index, line in enumerate(section) if f"#{NUMBER}" in line]
    assert starts, f"#{NUMBER} must have an entry under {heading!r}:\n{output}"
    entry = [section[starts[0]]]
    for line in section[starts[0] + 1:]:
        if line.startswith("- "):
            break
        entry.append(line)
    return "\n".join(entry), "\n".join(section)


def test_full_report_shows_the_whole_stalled_reason_as_plain_text(stalled_report):
    case, language = stalled_report
    output = case.brief("--report", "--full")
    entry, section = _stalled_entry(output, language)
    assert "**" not in entry, f"The stalled reason must be plain text, without markdown markers:\n{output}"
    assert "…" not in entry, f"The stalled reason must not be cut:\n{output}"
    assert PLAIN in _words(entry), (
        f"#{NUMBER}'s entry must carry the whole reason, bold line included:\n{output}"
    )
    assert _words(section).count(PLAIN) == 1, f"The reason must be shown once:\n{output}"
