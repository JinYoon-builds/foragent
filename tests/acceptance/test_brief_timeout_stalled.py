"""Plain `fa brief` shows timeout-only work as stalled, not as a decision (n86).

An open issue whose only current escalation is an orchestrator timeout is stalled
work (멈춤 in Korean). A genuine person decision keeps its decision wording and a
PR awaiting review keeps its review wording. Everything runs through `fa brief`
without --report, from local state only.
"""

from datetime import UTC, datetime, timedelta
import re

import pytest
import yaml

NOW = datetime(2026, 11, 5, 3, tzinfo=UTC)
TIMEOUT, PERSON, REVIEW = 20, 21, 22
TITLES = {TIMEOUT: "Wait for CI", PERSON: "Review policy", REVIEW: "Ship parser"}
STALLED = {"en": re.compile(r"\bstalled\b", re.I), "ko": re.compile("멈춤")}
DECISION = {"en": re.compile(r"decision|decide", re.I), "ko": re.compile("결정")}
TIMEOUT_BODY = ("<!-- foragent:needs-human reason=orchestrator-timeout -->\n"
                "Orchestrator timeout: no response to this blocker within 30 minutes.\n\n"
                "Latest blocker:\n<!-- foragent:blocked reason=timeout -->\nCI did not finish")


def _comment(cid, at, body):
    return {"id": cid, "created_at": at.isoformat(), "body": body, "author": "foragent"}


@pytest.fixture
def brief_world(tmp_path, monkeypatch, capsys):
    from foragent import cli
    from foragent.state import StateStore
    from foragent.tracker import Issue, PullRequest

    store = StateStore(tmp_path / "state", now=lambda: NOW)
    store.root.mkdir(parents=True)
    root = tmp_path / "project"
    root.mkdir()

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW.astimezone(tz) if tz else NOW.replace(tzinfo=None)

    monkeypatch.setattr(cli, "datetime", Clock)
    monkeypatch.setattr(cli.StateStore, "for_repo", classmethod(lambda cls, repo, **kwargs: store))
    monkeypatch.setattr(cli.update_check, "check", lambda config: None)
    monkeypatch.setattr(cli.update_check, "cached", lambda config: None)
    monkeypatch.setattr(cli.star, "offer", lambda language: None)

    def run(language, *, person=True, review=True, older_timeout_on_person=False):
        (root / ".foragent.yml").write_text(
            yaml.safe_dump({"repo": "offline/brief-stalled", "language": language}), encoding="utf-8")
        held = frozenset({"needs-human", "blocked"})
        early, late = NOW - timedelta(minutes=40), NOW - timedelta(minutes=5)
        issues = [Issue(TIMEOUT, TITLES[TIMEOUT], "", held, "open")]
        comments = {TIMEOUT: [_comment(1, early, "<!-- foragent:blocked reason=timeout -->\nCI did not finish"),
                              _comment(2, late, TIMEOUT_BODY)]}
        prs = []
        if person:
            issues.append(Issue(PERSON, TITLES[PERSON], "", held, "open"))
            # The older orchestrator timeout is superseded by a genuine person question.
            comments[PERSON] = ([_comment(3, early, TIMEOUT_BODY)] if older_timeout_on_person else []) + [
                _comment(4, late, "<!-- foragent:needs-human reason=review-dispute -->\nPick one")]
        if review:
            issues.append(Issue(REVIEW, TITLES[REVIEW], "", frozenset({"approved"}), "open"))
            prs.append(PullRequest(90, f"foragent/issue-{REVIEW}", "main", TITLES[REVIEW], f"Closes #{REVIEW}"))
        store.refresh_local_issues(issues, prs, {}, NOW, comments=comments)
        capsys.readouterr()
        assert cli.main(["brief", "--repo-root", str(root)]) == 0
        return capsys.readouterr().out

    return run


def _issue_lines(output, number):
    return [line for line in output.splitlines() if re.search(rf"#{number}(?!\d)", line)]


def _heading_above(output, line):
    """The nearest preceding non-item line: the section the item sits in."""
    lines = output.splitlines()
    for previous in reversed(lines[:lines.index(line)]):
        if not previous.lstrip().startswith("-"):
            return previous
    return ""


def _assert_stalled(output, language, number=TIMEOUT):
    lines = _issue_lines(output, number)
    assert len(lines) == 1, f"#{number} must be listed exactly once in the plain brief:\n{output}"
    line = lines[0]
    assert TITLES[number] in line, f"#{number}'s line must carry its title:\n{output}"
    section = _heading_above(output, line)
    assert STALLED[language].search(line) or STALLED[language].search(section), (
        f"#{number} must be shown as stalled ({STALLED[language].pattern!r}), in its line or section:\n{output}")
    assert not DECISION[language].search(line), f"#{number} must not be described as needing a decision:\n{output}"
    assert not DECISION[language].search(section) or STALLED[language].search(section), (
        f"#{number} must not sit under a decision heading:\n{output}")


@pytest.mark.parametrize("language", ["en", "ko"])
def test_a_timeout_only_issue_is_stalled_in_the_plain_brief(brief_world, language):
    output = brief_world(language, person=False, review=False)
    _assert_stalled(output, language)


@pytest.mark.parametrize("language", ["en", "ko"])
@pytest.mark.parametrize("older_timeout_on_person", [False, True])
def test_stalled_decision_and_review_each_keep_their_own_wording(brief_world, language, older_timeout_on_person):
    from foragent.i18n import text

    output = brief_world(language, older_timeout_on_person=older_timeout_on_person)
    _assert_stalled(output, language)

    decision, review = text("brief_kind_needs_human", language), text("brief_kind_pr", language)
    person = _issue_lines(output, PERSON)
    assert len(person) == 1 and TITLES[PERSON] in person[0], f"#{PERSON} must be listed once:\n{output}"
    assert decision in person[0], f"A genuine person decision keeps its decision wording:\n{output}"
    assert not STALLED[language].search(person[0]), f"#{PERSON} is a decision, not stalled work:\n{output}"

    pr = _issue_lines(output, REVIEW)
    assert len(pr) == 1 and TITLES[REVIEW] in pr[0], f"#{REVIEW} must be listed once:\n{output}"
    assert review in pr[0], f"The PR awaiting review keeps its review wording:\n{output}"
    assert not STALLED[language].search(pr[0]) and decision not in pr[0], output
