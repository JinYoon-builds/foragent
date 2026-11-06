"""Boundaries of the gate's durable preread/approval worker (#1488) beyond the locked acceptance file."""

from datetime import UTC, datetime
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent / "acceptance"))

from foragent import order, order_jobs  # noqa: E402
from foragent.config import load_config  # noqa: E402
from foragent.scheduler import TickContext, tick  # noqa: E402
from foragent.state import StateStore  # noqa: E402
from test_one_entrance_seat import enter, invoke, make_pm_case  # noqa: E402
from test_skills_autosync import isolated_home  # noqa: E402,F401


def _tick(case):
    tick(TickContext(repo=case.values["repo"], repo_root=case.root, tracker=case.tracker,
                     store=StateStore(case.store.root), config=load_config(case.root), git=case.git,
                     now=lambda: datetime.now(UTC), launcher=lambda n: 90000 + n,
                     runner_process_exists=lambda n: True))


def _submit(case, monkeypatch, capsys, *flags):
    enter(monkeypatch, capsys, "unit-pm")
    result = invoke(capsys, "order", "preread", case.path, "--background", *flags, "--json")
    assert result.code == 0, result.text
    monkeypatch.delenv("FORAGENT_SESSION_ID")


def _counting_publish(monkeypatch):
    calls = []
    real = order.publish
    monkeypatch.setattr(order, "publish", lambda *a, **k: calls.append(k.get("approve")) or real(*a, **k))
    return calls


def test_a_finished_job_is_not_published_again_on_later_ticks(tmp_path, monkeypatch, capsys, isolated_home):
    case = make_pm_case(tmp_path, monkeypatch)
    _submit(case, monkeypatch, capsys, "--approve-if-clean")
    publishes = _counting_publish(monkeypatch)
    for _ in range(3):
        _tick(case)
    assert publishes == [True], "Only the first tick may publish; later ticks see a finished job"
    assert [job["state"] for job in order_jobs.jobs(case.store)] == ["approved"]


def test_a_clean_job_without_authorization_ends_clean_and_publishes_nothing(
    tmp_path, monkeypatch, capsys, isolated_home,
):
    case = make_pm_case(tmp_path, monkeypatch)
    _submit(case, monkeypatch, capsys)
    publishes = _counting_publish(monkeypatch)
    _tick(case)
    assert publishes == [] and len(case.calls) == 1
    assert [job["state"] for job in order_jobs.jobs(case.store)] == ["clean"]
    assert not case.tracker.list_issues()


def test_changed_content_after_submission_is_not_approved(tmp_path, monkeypatch, capsys, isolated_home):
    case = make_pm_case(tmp_path, monkeypatch)
    _submit(case, monkeypatch, capsys, "--approve-if-clean")
    real = order.validate_plan
    monkeypatch.setattr(order, "validate_plan",
                        lambda *a, **k: {**real(*a, **k), "tests/acceptance/test_pm_fixture.py": "changed\n"})
    _tick(case)
    assert case.calls == [], "Changed content gets no agent turn"
    assert not any("approved" in issue.labels for issue in case.tracker.list_issues())
    [job] = order_jobs.jobs(case.store)
    assert job["state"] == "failed" and job["events"][-1]["reason"] == "content-changed"


def _owner_publishes(case, request_status="published"):
    """Stand in for the merge owner: publish the queued request, then mark it as the owner does."""
    from dataclasses import replace

    from foragent import order_publications

    [row] = order_publications.requests(case.store)
    if request_status == "published":
        config = replace(load_config(case.root), merge_owner=False)
        order.publish(row["plan"], case.root, config, case.tracker, repo=case.values["repo"], git=case.git,
                      approve=True)
    row.update(status=request_status, reason=None if request_status == "published" else "base moved")
    order_publications._save(case.store, row)


def _merge_owner_case(tmp_path, monkeypatch):
    import yaml

    from foragent import merge_owner

    case = make_pm_case(tmp_path, monkeypatch)
    case.values["merge_owner"] = True
    case.values["test_command"] = "uv run pytest -q tests/acceptance"
    (case.root / ".foragent.yml").write_text(yaml.safe_dump(case.values))
    monkeypatch.setattr(merge_owner, "start", lambda *args: None)  # The owner's own tick is simulated.
    from foragent import scheduler

    monkeypatch.setattr(scheduler, "prebuild_acceptance_baseline", lambda *args: None)  # Not under test.
    return case


def _evidence(case, number):
    return [c.body for c in case.tracker.list_comments(number) if c.body.startswith("<!-- foragent:order-job ")]


def test_a_job_the_merge_owner_publishes_finishes_with_evidence(tmp_path, monkeypatch, capsys, isolated_home):
    case = _merge_owner_case(tmp_path, monkeypatch)
    _submit(case, monkeypatch, capsys, "--approve-if-clean")
    _tick(case)
    assert [job["state"] for job in order_jobs.jobs(case.store)] == ["queued"]
    _tick(case)  # Still pending: the job waits and nothing is written.
    assert [job["state"] for job in order_jobs.jobs(case.store)] == ["queued"] and not case.tracker.list_issues()
    _owner_publishes(case)
    _tick(case)
    _tick(case)
    [job] = order_jobs.jobs(case.store)
    [child] = [i for i in case.tracker.list_issues() if i.title == "Keep locale"]
    assert job["state"] == "approved" and job["events"][-1]["issues"] == [child.number]
    assert len(_evidence(case, child.number)) == 1 and "NONE" in _evidence(case, child.number)[0]
    assert len(case.calls) == 1, "The owner path must not ask the agent again"


def test_a_publication_returned_to_preparation_ends_the_job_without_approval(
    tmp_path, monkeypatch, capsys, isolated_home,
):
    case = _merge_owner_case(tmp_path, monkeypatch)
    _submit(case, monkeypatch, capsys, "--approve-if-clean")
    _tick(case)
    _owner_publishes(case, request_status="preparation")
    _tick(case)
    [job] = order_jobs.jobs(case.store)
    assert job["state"] == "failed" and job["events"][-1]["reason"] == "base moved"
    assert not any("approved" in issue.labels for issue in case.tracker.list_issues())


def test_a_question_on_an_existing_child_is_recorded_on_that_issue(tmp_path, monkeypatch, capsys, isolated_home):
    case = make_pm_case(tmp_path, monkeypatch)
    order.publish(case.plan, case.root, load_config(case.root), case.tracker, repo=case.values["repo"],
                  git=case.git)  # Published earlier without approval.
    [child] = [i for i in case.tracker.list_issues() if i.title == "Keep locale"]
    case.worker.replies = ["QUESTION: Which storage region does Jin want?"]
    _submit(case, monkeypatch, capsys, "--approve-if-clean")
    _tick(case)
    _tick(case)
    assert [job["state"] for job in order_jobs.jobs(case.store)] == ["question"]
    [evidence] = _evidence(case, child.number)
    assert "Which storage region" in evidence
    assert "approved" not in case.tracker.get_issue(child.number).labels


def test_a_retry_after_a_lost_finish_does_not_post_the_answer_twice(tmp_path, monkeypatch, capsys, isolated_home):
    case = make_pm_case(tmp_path, monkeypatch)
    _submit(case, monkeypatch, capsys, "--approve-if-clean")
    real, lost = order_jobs.record, []

    def record(store, job, event, **facts):
        if event == "approved" and not lost:
            lost.append(event)
            raise OSError("disk full")
        return real(store, job, event, **facts)

    monkeypatch.setattr(order_jobs, "record", record)
    try:
        _tick(case)
    except OSError:
        pass
    _tick(case)
    [child] = [i for i in case.tracker.list_issues() if i.title == "Keep locale"]
    assert lost and [job["state"] for job in order_jobs.jobs(case.store)] == ["approved"]
    assert len(_evidence(case, child.number)) == 1


def _two_children(case, *, existing):
    """A plan with two children; with ``existing`` both are published first, unapproved."""
    import json

    second = {**case.plan["issues"][0], "key": "more", "title": "Keep locale too", "goal": "Keep it twice"}
    second.pop("acceptance_files", None)
    case.plan["issues"].append(second)
    case.path.write_text(json.dumps(case.plan))
    if existing:
        order.publish(case.plan, case.root, load_config(case.root), case.tracker, repo=case.values["repo"],
                      git=case.git)
    return {i.title: i.number for i in case.tracker.list_issues()}


def test_a_failed_second_reader_keeps_the_first_question_and_its_answer(
    tmp_path, monkeypatch, capsys, isolated_home,
):
    from foragent import orch_inbox
    from foragent.executors import ExecResult

    case = make_pm_case(tmp_path, monkeypatch)
    numbers = _two_children(case, existing=True)
    replies = iter([ExecResult(0, False, "QUESTION: Which storage region does Jin want?", None),
                    ExecResult(1, False, "", None)])
    monkeypatch.setattr(case.worker, "run", lambda **kwargs: case.calls.append(kwargs) or next(replies))
    _submit(case, monkeypatch, capsys, "--approve-if-clean")
    _tick(case)
    _tick(case)
    [job] = order_jobs.jobs(case.store)
    assert job["state"] == "question" and len(case.calls) == 2, "One attempt, and the answer that came is kept"
    assert any("Which storage region" in q["question"] for q in orch_inbox.questions(case.store))
    [evidence] = _evidence(case, numbers["Keep locale"])
    assert "Which storage region" in evidence
    assert not _evidence(case, numbers["Keep locale too"])[1:], "The failed reader gets at most its outcome"
    assert not any("approved" in issue.labels for issue in case.tracker.list_issues())


def test_a_failed_preread_without_questions_is_retried_then_ends_with_its_reason(
    tmp_path, monkeypatch, capsys, isolated_home,
):
    from foragent import orch_jobs
    from foragent.executors import ExecResult

    case = make_pm_case(tmp_path, monkeypatch)
    monkeypatch.setattr(case.worker, "run", lambda **kwargs: case.calls.append(kwargs) or ExecResult(1, False, "", None))
    _submit(case, monkeypatch, capsys, "--approve-if-clean")
    for _ in range(orch_jobs.MAX_ATTEMPTS + 2):
        _tick(case)
    [job] = order_jobs.jobs(case.store)
    assert len(case.calls) == orch_jobs.MAX_ATTEMPTS
    assert job["state"] == "failed" and "did not finish with an answer" in job["events"][-1]["reason"]


def test_a_returned_publication_records_the_answers_and_the_reason_on_existing_issues(
    tmp_path, monkeypatch, capsys, isolated_home,
):
    case = _merge_owner_case(tmp_path, monkeypatch)
    order.publish(case.plan, case.root, __import__("dataclasses").replace(load_config(case.root), merge_owner=False),
                  case.tracker, repo=case.values["repo"], git=case.git)
    numbers = {i.title: i.number for i in case.tracker.list_issues()}
    _submit(case, monkeypatch, capsys, "--approve-if-clean")
    _tick(case)
    _owner_publishes(case, "preparation")
    _tick(case)
    [job] = order_jobs.jobs(case.store)
    assert job["state"] == "failed"
    [answer] = _evidence(case, numbers["Keep locale"])
    assert "NONE" in answer
    [outcome] = _evidence(case, numbers[case.plan["title"]])
    assert "base moved" in outcome


def test_a_refused_direct_publication_records_its_reason_on_the_order_issue(
    tmp_path, monkeypatch, capsys, isolated_home,
):
    case = make_pm_case(tmp_path, monkeypatch)
    order.publish(case.plan, case.root, load_config(case.root), case.tracker, repo=case.values["repo"],
                  git=case.git)
    numbers = {i.title: i.number for i in case.tracker.list_issues()}
    _submit(case, monkeypatch, capsys, "--approve-if-clean")

    def refuse(*args, **kwargs):
        raise order.PlanError("the base branch moved under the order")

    monkeypatch.setattr(order, "publish", refuse)
    _tick(case)
    [job] = order_jobs.jobs(case.store)
    assert job["state"] == "failed"
    [outcome] = _evidence(case, numbers[case.plan["title"]])
    assert "the base branch moved under the order" in outcome
    assert "NONE" in _evidence(case, numbers["Keep locale"])[0]
