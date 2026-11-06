"""Background approval follows the merge owner's durable publication outcome."""

from datetime import UTC, datetime
import json

import pytest
import yaml

from foragent import cli, merge_owner, order, order_prepare_review, order_publications
from foragent.config import load_config
from foragent.runner import RunContext
from foragent.scheduler import TickContext, TickReport, tick
from foragent.state import StateStore
from test_one_entrance_seat import enter, invoke, make_pm_case
from test_order_publications import OwnerGit
from test_skills_autosync import isolated_home  # noqa: F401


def apply_publications(case):
    store = StateStore(case.store.root)
    owner = RunContext(repo=case.values["repo"], repo_root=case.root, tracker=case.tracker,
                       store=store, config=load_config(case.root), git=case.git,
                       now=lambda: datetime.now(UTC))
    with store.lock_acceptance_baseline("merge-owner", blocking=False) as acquired:
        assert acquired is not None
        assert order_publications.apply_next(owner, TickReport())


def gate_tick(case):
    return tick(TickContext(repo=case.values["repo"], repo_root=case.root, tracker=case.tracker,
                            store=StateStore(case.store.root), config=load_config(case.root), git=case.git,
                            now=lambda: datetime.now(UTC), launcher=lambda n: None))


@pytest.fixture
def merge_owner_case(tmp_path, monkeypatch, isolated_home):
    case = make_pm_case(tmp_path, monkeypatch)
    case.values.update(merge_owner=True, test_command="uv run pytest")
    (case.root / ".foragent.yml").write_text(yaml.safe_dump(case.values))
    case.git = OwnerGit(case.root, case.events)
    case.git.remote_slug = lambda: case.values["repo"]
    case.git.remote.snapshots[case.git.head_sha()] = {
        ".foragent.yml": (case.root / ".foragent.yml").read_text(),
    }
    monkeypatch.setattr(cli, "GitRepo", lambda root: case.git)
    case.git.blob_sha = lambda ref, path: path if (case.root / path).is_file() else None
    git_call = case.git._call

    def read_git(args):
        if args[:2] == ["cat-file", "blob"]:
            return (case.root / args[2]).read_text()
        return git_call(args)

    case.git._call = read_git
    # Run the owner synchronously below with the offline Git and tracker adapters.
    monkeypatch.setattr(merge_owner, "start", lambda *args: None)
    case.verdict = {"status": "reviewed", "reason": "Still red on current main"}

    def review(plan, item, root, config, git, path, record, store, *, files=None):
        record["issues"][item["key"]].update(case.verdict)

    monkeypatch.setattr(order_prepare_review, "review", review)
    # Give both outcomes an existing order issue to receive the job result.
    # Publish it unapproved through the same owner path, then queue a revision.
    report = order.publish(case.plan, case.root, load_config(case.root), case.tracker,
                           repo=case.values["repo"], git=case.git)
    assert report.publication_request
    apply_publications(case)
    initial, = order_publications.requests(case.store)
    assert initial["status"] == "published", initial
    case.order_issue, = [issue for issue in case.tracker.list_issues()
                         if issue.title == case.plan["title"]]
    assert not any("approved" in issue.labels for issue in case.tracker.list_issues())
    case.plan["summary"] = "Keep locale after the PM session exits"
    case.path.write_text(json.dumps(case.plan))
    return case


def queue_and_tick(case, monkeypatch, capsys):
    assert enter(monkeypatch, capsys, "departed-merge-owner-pm").code == 0
    result = invoke(capsys, "order", "preread", case.path,
                    "--background", "--approve-if-clean", "--json")
    assert result.code == 0, result.text
    job = json.loads(result.out)["job"]["job"]
    assert case.calls == [], "Submitting the job must not run the agent in the PM shell"
    case.path.unlink()
    monkeypatch.delenv("FORAGENT_SESSION_ID")
    gate_tick(case)
    pending = order_publications.pending(StateStore(case.store.root))
    assert len(pending) == 1, "The gate must preread the queued job and request publication"
    request, = pending
    assert request["plan"] == case.plan and request["approve"]
    context = invoke(capsys, "order", "context", "--json")
    assert context.code == 0, context.text
    assert any(row["job"] == job for row in json.loads(context.out)["background_work"]), (
        "A publication request alone must not finish the job"
    )
    assert not any("approved" in issue.labels for issue in case.tracker.list_issues())
    return job, request["id"]


def assert_job_finished(case, capsys, job):
    result = invoke(capsys, "order", "context", "--json")
    assert result.code == 0, result.text
    assert not any(row["job"] == job for row in json.loads(result.out)["background_work"]), (
        "The job must finish after the merge owner resolves its publication request"
    )


def test_job_completes_when_the_merge_owner_publishes_its_request(
    merge_owner_case, monkeypatch, capsys,
):
    case = merge_owner_case
    job, request_id = queue_and_tick(case, monkeypatch, capsys)
    apply_publications(case)
    request, = [row for row in order_publications.requests(StateStore(case.store.root))
                if row["id"] == request_id]
    assert request["status"] == "published", request
    gate_tick(case)
    assert_job_finished(case, capsys, job)
    children = [issue for issue in case.tracker.list_issues() if issue.title == "Keep locale"]
    assert len(children) == 1 and "approved" in children[0].labels
    comments = [comment.body for comment in case.tracker.list_comments(case.order_issue.number)]
    assert any(all(f"#{issue.number}" in comment for issue in children) for comment in comments), (
        "The order issue must record the published issue numbers"
    )


def test_job_completes_when_the_merge_owner_returns_its_request(
    merge_owner_case, monkeypatch, capsys,
):
    case = merge_owner_case
    job, request_id = queue_and_tick(case, monkeypatch, capsys)
    reason = "The locale test now passes on current main; prepare a failing acceptance test"
    case.verdict.update(status="hold", reason=reason)
    apply_publications(case)
    request, = [row for row in order_publications.requests(StateStore(case.store.root))
                if row["id"] == request_id]
    assert request["status"] == "preparation" and reason in request["reason"], request
    gate_tick(case)
    assert_job_finished(case, capsys, job)
    comments = [comment.body for comment in case.tracker.list_comments(case.order_issue.number)]
    assert any(request["reason"] in comment for comment in comments), (
        "The order issue must record why the merge owner returned the request"
    )
    assert not any("approved" in issue.labels for issue in case.tracker.list_issues())
