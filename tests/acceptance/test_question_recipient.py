"""Acceptance for question-recipient. Locked by Foragent; do not edit.

Use real question/inbox and run facts, the public plain lane frame and statusline CLI.
No projection, question-selection helper or rendered text is substituted.
Each test names the wrong implementation it rejects.
"""

from datetime import UTC, datetime, timedelta
from io import BytesIO
import json
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
from test_squirrel_running import plain_frame, utf8_terminal, view_offline  # noqa: F401


@pytest.fixture(params=["ko", "en"])
def question_case(request, tmp_path, monkeypatch):
    from foragent import statusline

    case = offline_local_cli(tmp_path, monkeypatch)
    case.language = request.param
    (case.root / ".foragent.yml").write_text(yaml.safe_dump({
        "repo": "owner/repo", "language": case.language, "skill_libraries": [],
        "notify": "none", "lanes": {name: {"paths": [f"{name}/**"]}
                                     for name in ("person", "spec")},
    }), encoding="utf-8")
    case.config = load_config(case.root)
    case.clock = [datetime(2026, 10, 31, 12, tzinfo=UTC)]
    case.store = StateStore(case.store.root, now=lambda: case.clock[0])
    case.tracker = FakeTracker(now=lambda: case.clock[0])
    case.person = "진님" if case.language == "ko" else "you"
    case.orchestrator = "오케스트레이터" if case.language == "ko" else "the orchestrator"

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return case.clock[0].astimezone(tz) if tz else case.clock[0].replace(tzinfo=None)

    monkeypatch.setattr(statusline, "datetime", Clock)
    monkeypatch.delenv(statusline.NESTED_ENV, raising=False)
    return case


@pytest.fixture(params=["lane-view", "statusline"])
def surface(request):
    return request.param


def seed_spec_question(case, number, lane):
    case.tracker.add_issue(number, f"Storage region {number}",
                           labels=["approved", "blocked", f"lane:{lane}"])
    case.store.start_run(number, branch=f"foragent/issue-{number}",
                         worktree=case.store.worktree_path(number), lane=lane)
    case.store.update_run(number, phase="implementing")
    case.store.save_flag(number, "spec-question", flagged_at=case.clock[0].isoformat())
    case.tracker.comment(number, "<!-- foragent:blocked reason=spec-question -->\nChoose a storage region.")
    case.store.finish_run(number, outcome="spec-question", detail="Choose a storage region.")


def ask_person(case, capsys, number):
    capsys.readouterr()
    assert cli_exit(case.main, [
        "orch", "ask", "--question", f"Choose a storage region for #{number}.",
        "--issue", str(number), "--option", "Seoul", "--option", "Tokyo", "--recommend", "1",
        "--json", "--repo-root", str(case.root),
    ]) == 0
    return json.loads(capsys.readouterr().out)["id"]


def render(case, surface, monkeypatch):
    from foragent import status, watch

    case.store.refresh_local_issues(case.tracker.list_issues("all"), [],
                                    case.config.lanes, case.clock[0])
    if surface == "lane-view":
        payload = status.collect(case.store, case.config, case.clock[0])
        snapshot = watch.collect(payload, case.tracker, case.store, case.config, case.clock[0])
        value = plain_frame(watch.render_frame(snapshot, case.config, case.root,
                                                width=180, now=case.clock[0]))
    else:
        output = BytesIO()
        with monkeypatch.context() as streams:
            streams.setattr(sys, "stdin", SimpleNamespace(buffer=BytesIO(b"{}\n")))
            streams.setattr(sys, "stdout", SimpleNamespace(buffer=output))
            code = cli_exit(case.main, ["statusline"])
        assert code == 0, "The public statusline command must succeed"
        value = plain_frame(output.getvalue().decode("utf-8"))
    assert case.gh_calls == [], "Question display must read local facts without GitHub calls"
    return value


def has_name(text, name):
    if name in {"진님", "오케스트레이터"}:
        return name in text
    return re.search(rf"(?<!\w){re.escape(name)}(?!\w)", text.casefold()) is not None


def assert_recipient(text, surface, number, expected, other):
    if surface == "lane-view":
        lines = [line for line in text.splitlines() if re.search(rf"#{number}\b", line)]
        assert lines, f"The lane view must show the open question for #{number}: {text}"
        assert any(has_name(line, expected) for line in lines), (
            f"The question for #{number} must name {expected!r}: {lines}"
        )
        assert not any(has_name(line, other) for line in lines), (
            f"The question for #{number} must not name the other recipient: {lines}"
        )
    else:
        # A compact statusline may group counts by recipient instead of listing issue numbers.
        assert has_name(text, expected), f"The statusline must name {expected!r}: {text}"


def assert_question_gone(text, surface, number, case):
    if surface == "lane-view":
        lines = [line for line in text.splitlines() if re.search(rf"#{number}\b", line)]
        assert not any(re.search(r"질문|\bquestions?\b", line.casefold())
                       or has_name(line, case.person) or has_name(line, case.orchestrator)
                       for line in lines), f"#{number} must no longer appear as a question: {lines}"
    else:
        assert not re.search(r"질문|물어볼|\bquestions?\b", text.casefold()), text
        assert not has_name(text, case.person) and not has_name(text, case.orchestrator), text


@pytest.mark.parametrize("person_route", ["inbox", "needs-human"])
def test_each_question_names_its_own_recipient(question_case, surface, person_route, monkeypatch, capsys):
    """Reject unlabelled questions, treating all questions as the person's, or ignoring inbox routing."""
    case = question_case
    seed_spec_question(case, 1059, "person")
    seed_spec_question(case, 1060, "spec")
    if person_route == "inbox":
        ask_person(case, capsys, 1059)
    else:
        case.tracker.add_labels(1059, ["needs-human"])
    text = render(case, surface, monkeypatch)
    assert_recipient(text, surface, 1059, case.person, case.orchestrator)
    assert_recipient(text, surface, 1060, case.orchestrator, case.person)


def test_answered_question_disappears(question_case, surface, monkeypatch, capsys):
    """Reject retaining an answered inbox question or relabelling its old spec record as unanswered."""
    case = question_case
    seed_spec_question(case, 1059, "person")
    question_id = ask_person(case, capsys, 1059)
    before = render(case, surface, monkeypatch)
    assert cli_exit(case.main, ["orch", "answer", question_id, "--text", "Use Seoul.",
                                "--repo-root", str(case.root)]) == 0
    # Keep the older spec-question flag and comment: the recorded answer must win.
    after = render(case, surface, monkeypatch)
    assert_question_gone(after, surface, 1059, case)
    assert_recipient(before, surface, 1059, case.person, case.orchestrator)
    if surface == "statusline":
        assert not has_name(before, case.orchestrator), before


@pytest.mark.parametrize("outcome", ["retried", "merged"])
def test_old_spec_question_disappears_after_run_moves_on(question_case, surface, outcome, monkeypatch, capsys):
    """Reject showing an old spec question after retry/merge or hiding a fresh question with it."""
    case = question_case
    seed_spec_question(case, 1060, "spec")
    before = render(case, surface, monkeypatch)
    case.clock[0] += timedelta(minutes=1)
    case.tracker.remove_labels(1060, ["blocked", "needs-human"])
    case.store.start_run(1060, branch="foragent/issue-1060",
                         worktree=case.store.worktree_path(1060), lane="spec")
    case.store.update_run(1060, phase="implementing")
    if outcome == "merged":
        case.tracker.close_issue(1060)
        case.store.record_merge(1060, case.clock[0])
        case.store.finish_run(1060, outcome="merged")
    else:
        case.tracker.add_labels(1060, ["running"])
    # The older spec-question flag, comment and run history deliberately remain.
    seed_spec_question(case, 1059, "person")
    ask_person(case, capsys, 1059)
    after = render(case, surface, monkeypatch)
    if surface == "lane-view":
        assert_question_gone(after, surface, 1060, case)
    else:
        assert not has_name(after, case.orchestrator), after
    assert_recipient(after, surface, 1059, case.person, case.orchestrator)
    assert_recipient(before, surface, 1060, case.orchestrator, case.person)
    if surface == "statusline":
        assert not has_name(before, case.person), before
