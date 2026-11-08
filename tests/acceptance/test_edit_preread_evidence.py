"""Background preread/approval jobs record each edit's lane answer on the edited issue (n68).

The gate's job worker already comments each reader's answer on existing new-plan children.
These checks drive the same public path (``fa order preread --background``, then gate ticks)
with plans that edit an existing issue, alone or beside a new child, using an offline
tracker and a fake lane agent. Each reader's reply carries a unique token so the evidence
on every issue can be attributed to the reader that wrote it.
"""

import json
from datetime import UTC, datetime

from foragent import order, order_jobs
from foragent.config import load_config
from foragent.scheduler import TickContext, tick
from foragent.state import StateStore
from test_one_entrance_seat import enter, invoke, make_pm_case
from test_skills_autosync import isolated_home  # noqa: F401

EDIT_TOKEN = "edit-reader-alpha-7f3c"
CHILD_TOKEN = "child-reader-beta-91d2"
QUESTION = "Which storage region should the edited issue keep?"


def _tick(case):
    tick(TickContext(repo=case.values["repo"], repo_root=case.root, tracker=case.tracker,
                     store=StateStore(case.store.root), config=load_config(case.root), git=case.git,
                     now=lambda: datetime.now(UTC), launcher=lambda n: 90000 + n,
                     runner_process_exists=lambda n: True))


def _existing_issue(case):
    """Publish the fixture's first order without approval; its child is the issue to edit."""
    order.publish(case.plan, case.root, load_config(case.root), case.tracker, repo=case.values["repo"],
                  git=case.git)
    [target] = [i for i in case.tracker.list_issues() if i.title == "Keep locale"]
    assert "approved" not in target.labels
    return target.number


def _edit_plan(case, number, *, with_child=False):
    edit = {"number": number, "goal": "Keep the selected locale after a restart",
            "out_of_scope": "Other locales",
            "done_when": ["uv run pytest -q tests/acceptance/test_pm_fixture.py"]}
    issues = []
    if with_child:
        issues = [{"key": "fresh", "title": "Keep timezone", "goal": "Keep the selected timezone",
                   "out_of_scope": "Other settings", "lane": "api", "size": "s",
                   "done_when": ["uv run pytest -q tests/acceptance/test_pm_timezone.py"],
                   "acceptance_files": {"tests/acceptance/test_pm_timezone.py":
                                        "def test_timezone():\n    assert True\n"}}]
    plan = {"id": "2026-11-08-edit-fixture", "title": "Edit work", "summary": "Edit one task",
            "spec_markdown": "# Edit work\n", "issues": issues, "edits": [edit]}
    case.path.write_text(json.dumps(plan))
    return plan


def _submit(case, monkeypatch, capsys, identity):
    enter(monkeypatch, capsys, identity)
    result = invoke(capsys, "order", "preread", case.path, "--background", "--approve-if-clean", "--json")
    assert result.code == 0, "FA must accept durable preread/approval work: " + result.text
    monkeypatch.delenv("FORAGENT_SESSION_ID")
    [job] = [row for row in order_jobs.jobs(case.store) if row["title"] == "Edit work"]
    return job["job"]


def _job(case, identity):
    [job] = [row for row in order_jobs.jobs(case.store) if row["job"] == identity]
    return job


def _bodies(case, number, token):
    return [c.body for c in case.tracker.list_comments(number) if token in c.body]


def test_a_clean_edit_job_records_the_lane_answer_with_its_job_on_the_edited_issue(
    tmp_path, monkeypatch, capsys, isolated_home,
):
    case = make_pm_case(tmp_path, monkeypatch)
    number = _existing_issue(case)
    _edit_plan(case, number)
    case.worker.replies = [f"APPROACH: {EDIT_TOKEN}"]
    identity = _submit(case, monkeypatch, capsys, "edit-pm")
    for _ in range(2):
        _tick(case)
    assert len(case.calls) == 1, "The edit's lane reader runs once"
    assert _job(case, identity)["state"] == "approved"
    evidence = _bodies(case, number, EDIT_TOKEN)
    assert len(evidence) == 1, "The edited issue must receive the recorded lane answer once"
    assert identity[:12] in evidence[0], "The evidence must name the background job that recorded it"


def test_a_question_on_an_edit_is_recorded_on_its_target_and_not_approved(
    tmp_path, monkeypatch, capsys, isolated_home,
):
    case = make_pm_case(tmp_path, monkeypatch)
    number = _existing_issue(case)
    _edit_plan(case, number)
    case.worker.replies = [f"QUESTION: {QUESTION}"]
    identity = _submit(case, monkeypatch, capsys, "edit-question-pm")
    for _ in range(2):
        _tick(case)
    assert len(case.calls) == 1
    assert _job(case, identity)["state"] == "question", "An unresolved edit must not be approved"
    assert "approved" not in case.tracker.get_issue(number).labels
    evidence = _bodies(case, number, QUESTION)
    assert len(evidence) == 1, "The edited issue must receive the actual recorded question"
    assert identity[:12] in evidence[0]


def test_replaying_an_edit_jobs_completion_leaves_one_evidence_comment(
    tmp_path, monkeypatch, capsys, isolated_home,
):
    case = make_pm_case(tmp_path, monkeypatch)
    number = _existing_issue(case)
    _edit_plan(case, number)
    case.worker.replies = [f"APPROACH: {EDIT_TOKEN}"]
    identity = _submit(case, monkeypatch, capsys, "edit-replay-pm")
    real, lost = order_jobs.record, []

    def record(store, job, event, **facts):  # The first finish is lost after its comments.
        if event == "approved" and not lost:
            lost.append(event)
            raise OSError("disk full")
        return real(store, job, event, **facts)

    monkeypatch.setattr(order_jobs, "record", record)
    try:
        _tick(case)
    except OSError:
        pass
    for _ in range(3):
        _tick(case)
    assert lost and _job(case, identity)["state"] == "approved"
    assert len(case.calls) == 1, "A replayed completion must not ask the agent again"
    assert len(_bodies(case, number, EDIT_TOKEN)) == 1, "One evidence comment per job and edited issue"


def test_a_mixed_plan_puts_each_answer_only_on_its_own_issue(tmp_path, monkeypatch, capsys, isolated_home):
    case = make_pm_case(tmp_path, monkeypatch)
    number = _existing_issue(case)
    _edit_plan(case, number, with_child=True)
    # Readers run for the plan's new children first, then for its edits.
    case.worker.replies = [f"APPROACH: {CHILD_TOKEN}", f"APPROACH: {EDIT_TOKEN}"]
    identity = _submit(case, monkeypatch, capsys, "edit-mixed-pm")
    for _ in range(2):
        _tick(case)
    assert len(case.calls) == 2
    assert _job(case, identity)["state"] == "approved"
    [child] = [i for i in case.tracker.list_issues() if i.title == "Keep timezone"]
    assert len(_bodies(case, child.number, CHILD_TOKEN)) == 1, "The new child keeps its own answer"
    assert not _bodies(case, child.number, EDIT_TOKEN), "The edit's answer must not land on the new child"
    assert len(_bodies(case, number, EDIT_TOKEN)) == 1, "The edited issue receives its own answer"
    assert not _bodies(case, number, CHILD_TOKEN), "The child's answer must not land on the edited issue"
