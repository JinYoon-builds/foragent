"""Roles acceptance: facts and scorecard show decided issues waiting for tests and decision-to-approval time.

A decided issue waits for tests while it is open and its Done when is still the placeholder
command. Decision-to-approval time runs from the issue's creation on GitHub to the approval
the gate recorded. Missing evidence reads unknown (null in JSON), never zero.

GitHub is faked at the gh command boundary with GitHub's REST shapes; the gate tick, Git
history and both reading commands are real. Times are relative to the real clock.
"""

from datetime import UTC, datetime, timedelta
import json
import re
import subprocess

import pytest
import yaml

from conftest import git_command
from test_skills_autosync import cli_exit, isolated_home  # noqa: F401

PLACEHOLDER = "false  # placeholder: acceptance tests are written before this issue can be approved"
REAL = "uv run pytest -q tests/acceptance/test_feature.py"


def _body(command):
    return ("<!-- foragent:v1 -->\n## Goal\nChange the module.\n## Out of scope\nOther modules.\n"
            f"## Done when\n```sh\n{command}\n```\n")


def _stamp(moment):
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class FakeGitHub:
    """Answers the gh calls of one gate tick from a list of issues and their label events."""

    def __init__(self):
        self.issues, self.events, self.calls = [], {}, []

    def issue(self, number, command, *, created=None, approved=None, state="open", labels=()):
        row = {"number": number, "title": f"Issue {number}", "body": _body(command), "state": state,
               "labels": [{"name": name} for name in (*labels, *(("approved",) if approved else ()))],
               "updated_at": _stamp(approved or created or datetime.now(UTC))}
        if created is not None:
            row["created_at"] = _stamp(created)
        self.issues.append(row)
        if approved is not None:
            self.events[number] = [{"event": "labeled", "label": {"name": "approved"},
                                    "actor": {"login": "human"}, "created_at": _stamp(approved)}]

    def reply(self, args):
        path = next((arg for arg in args if arg.startswith("repos/") or arg == "graphql"), "")
        parts = path.split("?")[0].split("/")
        if path == "graphql":
            return json.dumps({"data": {"repository": {"issue": {"lastEditedAt": None}}}})
        if "/issues?state=all" in path:
            return json.dumps(self.issues)
        if "/issues?state=closed" in path:
            return json.dumps([row for row in self.issues if row["state"] == "closed"])
        if len(parts) == 5 and parts[3] == "issues" and parts[4].isdigit():
            return json.dumps(next(row for row in self.issues if row["number"] == int(parts[4])))
        if len(parts) == 6 and parts[5] == "events":
            return json.dumps(self.events.get(int(parts[4]), []))
        if parts[-1] == "comments" and "POST" in args:
            return json.dumps({"id": 900 + len(self.calls)})
        return "[]"


@pytest.fixture
def gate(git_history, isolated_home, monkeypatch, capsys):
    from foragent import cli

    now = datetime.now(UTC).replace(microsecond=0)
    git_history.update({".foragent.yml": yaml.safe_dump({
        "repo": "owner/repo", "language": "en", "approvers": ["human"], "skill_libraries": [],
        "test_command": "true"})}, at=now - timedelta(days=20))
    git_command(git_history.clone, "fetch", "-q", "origin")
    git_command(git_history.clone, "merge", "-q", "--ff-only", "origin/main")
    monkeypatch.chdir(git_history.clone)
    github = FakeGitHub()
    original = subprocess.run

    def run(argv, *args, **kwargs):
        if isinstance(argv, (list, tuple)) and argv and str(argv[0]) == "gh":
            github.calls.append(list(argv[1:]))
            return subprocess.CompletedProcess(argv, 0, stdout=github.reply(list(argv[1:])), stderr="")
        return original(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)

    def fa(*argv, reads_github=False):
        before = len(github.calls)
        code = cli_exit(cli.main, list(argv))
        output = capsys.readouterr()
        assert code == 0, f"fa {' '.join(argv)} exited {code}: {output.err.strip()}"
        if not reads_github:
            assert github.calls[before:] == [], f"fa {' '.join(argv)} must read local records only"
        return output.out

    github.now, github.fa = now, fa
    github.tick = lambda: fa("gate", "tick", "--no-start", "--json", reads_github=True)
    return github


def _facts(gate):
    return json.loads(gate.fa("orch", "facts", "--json"))


def _card(gate):
    return json.loads(gate.fa("orch", "scorecard", "--json"))


def _waiting(facts):
    value = facts.get("tests_waiting")
    assert isinstance(value, dict), "fa orch facts --json must expose tests_waiting"
    return value


def _approval(facts):
    value = facts.get("decision_to_approval")
    assert isinstance(value, dict), "fa orch facts --json must expose decision_to_approval"
    return value


def _today(card, name, window="today"):
    metrics = card["window"][window]
    assert name in metrics, f"fa orch scorecard --json must expose {name} in the {window} window"
    return metrics[name]


def _line(text, pattern):
    lines = [line for line in text.splitlines() if re.search(pattern, line, re.I)]
    assert lines, f"No line matching {pattern!r} in:\n{text}"
    return "\n".join(lines)


def _decided(gate):
    """Two waiting issues, issues that do not wait, and approvals today, yesterday and long ago."""
    now = gate.now
    gate.issue(11, PLACEHOLDER, created=now - timedelta(days=2))
    gate.issue(15, PLACEHOLDER, created=now - timedelta(hours=5), labels=("by:human",))
    gate.issue(12, REAL, created=now - timedelta(days=1))                 # Real Done when, not approved.
    gate.issue(16, PLACEHOLDER, created=now - timedelta(days=4), state="closed")
    gate.issue(13, REAL, created=now - timedelta(hours=30), approved=now - timedelta(hours=2))   # 1680 min
    gate.issue(17, REAL, created=now - timedelta(hours=4), approved=now - timedelta(hours=3))    # 60 min
    gate.issue(14, REAL, approved=now - timedelta(hours=1))                                      # unknown
    gate.issue(18, REAL, created=now - timedelta(hours=40), approved=now - timedelta(hours=30))  # 600 min
    gate.issue(19, REAL, created=now - timedelta(days=12), approved=now - timedelta(days=10))    # outside
    gate.tick()


def test_facts_count_open_placeholder_issues_and_time_known_approvals_in_the_window(gate):
    _decided(gate)
    facts = _facts(gate)

    waiting = _waiting(facts)
    assert waiting.get("count") == 2, waiting
    assert sorted(waiting.get("issues") or []) == [11, 15], "Only open issues whose Done when is the placeholder wait"

    approval = _approval(facts)
    # Approved within the 7-day window: 13 (1680), 17 (60), 18 (600) and 14 with no known creation.
    assert approval.get("approved") == 4, approval
    assert approval.get("unknown") == 1, "An approval without a known creation time is unknown, not zero"
    assert approval.get("median_minutes") == pytest.approx(600), approval


def test_scorecard_shows_waiting_now_and_decision_to_approval_per_window(gate):
    _decided(gate)
    card = _card(gate)

    assert _today(card, "tests_waiting") == 2
    assert _today(card, "decision_to_approval_minutes") == pytest.approx(870), "Median of 1680 and 60"
    assert _today(card, "decision_unknown") == 1
    assert _today(card, "decision_to_approval_minutes", "previous") == pytest.approx(600)


def test_both_human_views_show_the_two_numbers(gate):
    _decided(gate)
    facts = gate.fa("orch", "facts")
    assert re.search(r"\b2\b", _line(facts, r"wait\w* for tests|tests? wait")), facts
    approval = _line(facts, r"decision")
    assert re.search(r"\b600\b", approval) and re.search(r"unknown\D{0,3}1\b|\b1\b\D{0,3}unknown", approval, re.I), approval

    card = gate.fa("orch", "scorecard")
    assert re.search(r"\b2\b", _line(card, r"wait\w* for tests|tests? wait")), card
    assert re.search(r"\b870\b", _line(card, r"decision")), card


def test_missing_evidence_reads_unknown_never_zero(gate):
    # Before any GitHub listing nothing is known about open issues.
    facts = _facts(gate)
    assert _waiting(facts).get("count") is None, "No listing yet: the waiting count is unknown, not 0"
    assert _today(_card(gate), "tests_waiting") is None
    assert re.search(r"\?|unknown", _line(gate.fa("orch", "facts"), r"wait\w* for tests|tests? wait"), re.I)

    # One approval whose creation time GitHub did not give: the time is unknown, not 0 minutes.
    gate.issue(14, REAL, approved=gate.now - timedelta(hours=1))
    gate.tick()
    facts = _facts(gate)
    assert _waiting(facts).get("count") == 0, "A listing with no placeholder issue is a known zero"
    approval = _approval(facts)
    assert (approval.get("approved"), approval.get("unknown"), approval.get("median_minutes")) == (1, 1, None)
    card = _card(gate)
    assert _today(card, "decision_to_approval_minutes") is None
    assert _today(card, "decision_unknown") == 1
