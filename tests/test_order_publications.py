"""Publication admission and owner recovery with fake Git, reviewer and GitHub."""

from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime
import shutil
from types import SimpleNamespace

import pytest

from test_order import PublishingGit, PublishingTracker
from foragent import brief, merge_owner, order, order_collect, order_prepare, order_prepare_cycle
from foragent import order_prepare_review, order_prepare_setup, order_publications, order_reviewed, orch_inbox
from foragent.config import Config
from foragent.git import GitError
from foragent.scheduler import TickReport
from foragent.state import StateStore
from foragent.status import collect as status

NAME = "tests/acceptance/test_new.py"
COMMAND = f"uv run pytest {NAME}"


class OwnerGit(PublishingGit):
    def __init__(self, root, events, remote=None, head="main-1"):
        super().__init__(root, events)
        self.head = head
        self.remote = remote or SimpleNamespace(head=head, batch=False, lost=False, reject=False,
                                                parents={}, snapshots={}, starts={})

    def head_sha(self):
        return self.head

    def ref_sha(self, ref):
        return self.remote.head

    def commit_before(self, ref, iso_time):
        # The fake remote keeps no commit times: an approval reads the base tip it was published on.
        return self.remote.head

    def blob_shas(self, ref, paths):
        # A remote-tracking name reads the remote tip, as Git resolves it.
        ref = self.remote.head if ref.startswith("origin/") else ref
        saved = self.saved if ref == "HEAD" else self.remote.snapshots.get(ref, {})
        return {name: self._blob(saved[name]) if name in saved else "missing" for name in paths}

    def commit_paths(self, paths, title):
        super().commit_paths(paths, title)
        parent = self.head
        self.head = f"publication-{len(self.remote.parents) + 1}"
        self.remote.parents[self.head] = parent
        self.remote.snapshots[self.head] = dict(self.saved)

    def is_ancestor(self, ancestor, head):
        while True:
            if ancestor == head:
                return True
            if head not in self.remote.parents:
                return False
            head = self.remote.parents[head]

    def push_base(self, branch):
        self.events.append(("push", branch))
        if self.remote.reject:
            raise GitError("non-fast-forward")
        self.remote.head = self.head
        if self.remote.lost:
            self.remote.lost = False
            raise GitError("lost push response")

    def _call(self, args):
        if args[0] == "ls-remote":
            return "batch refs/heads/foragent/batch-merge-open" if self.remote.batch else ""
        if args[0] == "push":
            OwnerGit.push_base(self, args[-1].split("refs/heads/")[-1])
            return ""
        return super()._call(args)

    def add_detached_worktree(self, path, ref):
        path.mkdir()
        self.remote.starts[str(path)] = ref
        for name, data in self.remote.snapshots.get(ref, {}).items():
            destination = path / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(data)

    def at(self, root):
        git = OwnerGit(root, self.events, self.remote, self.remote.starts[str(root)])
        git.branch = None
        git.saved = dict(self.remote.snapshots.get(git.head, {}))
        return git

    def remove_worktree(self, root):
        shutil.rmtree(root)


@pytest.fixture
def case(tmp_path, monkeypatch):
    root = tmp_path / "caller"
    root.mkdir()
    config = Config(repo="owner/repo", merge_owner=True, test_command="uv run pytest")
    plan = {"id": "publication", "title": "Publication", "summary": "Goal", "spec_markdown": "# Spec",
            "issues": [{"key": "new", "title": "New behaviour", "goal": "Add observable behaviour",
                        "out_of_scope": "Other behaviour", "done_when": [COMMAND], "lane": "main", "size": "s",
                        "statements": ["The new behaviour works"], "acceptance_files": {NAME: "def test_new(): pass\n"}}]}
    store = StateStore.for_repo(config.repo)
    monkeypatch.setattr(order, "kick_tick", lambda root: None)
    monkeypatch.setattr(order, "require_clean_preread", lambda *args: None)
    monkeypatch.setattr(order_collect, "check", lambda *args, **kw: order_collect.Collected(args[4].head_sha(), 0))
    monkeypatch.setattr(order_collect, "land", lambda *args: None)
    monkeypatch.setattr(order_reviewed, "landing", lambda git, *args, **kw: git.head_sha())
    events = []
    git = OwnerGit(root, events)
    tracker = PublishingTracker(events)
    files = order.validate_plan(plan, root, config)
    entry = {"key": "new", "status": "reviewed", "phase": "reviewing_tests", "reason": "Red",
             "written": plan["issues"][0]["acceptance_files"], "done_when": [COMMAND], "base_revision": "main-1",
             "cycle": order_prepare_cycle.identity(plan, plan["issues"][0], config, root, files),
             "published": order_prepare_setup.published_digest(plan, root, config, files),
             "others": {}, "attempts": []}
    path = order_prepare._record_path(store, plan)
    path.parent.mkdir(parents=True)
    order_prepare._write(path, {"plan": plan["id"], "issues": {"new": entry}})
    checks = []
    verdict = {"status": "reviewed", "reason": "Still red on current main"}

    def review(plan, item, root, config, git, path, record, store, *, files=None):
        entry = record["issues"][item["key"]]
        checks.append((entry["base_revision"], deepcopy(entry["written"]), list(entry["done_when"]), files))
        entry.update(verdict)
        if verdict["status"] != "reviewed":
            entry["phase"] = "writing_tests"

    monkeypatch.setattr(order_prepare_review, "review", review)
    ctx = SimpleNamespace(repo=config.repo, repo_root=root, config=config, git=git, tracker=tracker,
                          store=store, now=lambda: datetime.now(UTC))
    return SimpleNamespace(root=root, config=config, plan=plan, store=store, git=git, tracker=tracker,
                           ctx=ctx, checks=checks, verdict=verdict, events=events)


def submit(case, **options):
    return order.publish(case.plan, case.root, case.config, case.tracker, repo=case.config.repo,
                         git=case.git, **({"approve": True} | options))


def review_plan(case):
    """Retain the fake review for the edited plan, as a fresh preparation would."""
    files = order.validate_plan(case.plan, case.root, case.config)
    path = order_prepare._record_path(case.store, case.plan)
    record = order_prepare._read(path, case.plan)
    for item in case.plan["issues"]:
        record["issues"][item["key"]].update(
            cycle=order_prepare_cycle.identity(case.plan, item, case.config, case.root, files),
            published=order_prepare_setup.published_digest(case.plan, case.root, case.config, files))
    order_prepare._write(path, record)


def test_admission_retains_bytes_and_returns_without_touching_main(case, monkeypatch):
    case.git.remote.batch = True
    monkeypatch.setattr(order_collect, "check", lambda *a, **kw: pytest.fail("Admission ran collection"))
    report = submit(case)
    assert report.publication_request
    assert "merge owner" in report.summary()
    row, = order_publications.pending(case.store)
    assert row["plan"] == case.plan and row["approve"]
    assert row["files"][NAME] == case.plan["issues"][0]["acceptance_files"][NAME]
    assert row["prepared"]["new"]["done_when"] == [COMMAND]
    assert not case.events and not case.checks
    assert case.git.head_sha() == case.git.remote.head == "main-1"
    assert not (case.root / NAME).exists()


def test_owner_publishes_between_batches_once_after_restart_and_main_move(case, monkeypatch):
    submit(case)
    case.git.remote.head = "main-2"
    case.ctx.store = StateStore(case.store.root)  # new process, same durable folder
    continued = []

    def admissions(store):
        continued.append([row["status"] for row in order_publications.requests(store)])
        return {}

    monkeypatch.setattr(merge_owner, "admissions", admissions)
    report = TickReport()
    merge_owner.tick(case.ctx, report)
    row, = order_publications.requests(case.store)
    assert row["status"] == "published"
    assert continued == [["published"]]
    assert case.checks[0][:3] == ("main-2", {NAME: "def test_new(): pass\n"}, [COMMAND])
    assert case.git.remote.head == row["commit"]
    assert len(case.tracker.list_issues()) == 2
    assert len([event for event in case.events if event[0] == "push"]) == 1
    assert not order_publications.apply_next(case.ctx, report)
    assert len(case.checks) == 1
    assert case.git.head_sha() == "main-1" and not (case.root / NAME).exists()


def test_open_batch_keeps_publication_pending_and_owner_advances_batch(case, monkeypatch):
    submit(case)
    case.git.remote.batch = True
    called = []
    monkeypatch.setattr(merge_owner, "admissions", lambda store: called.append("batch") or {})
    monkeypatch.setattr("foragent.merge_owner_batch.retire", lambda *args: None)
    merge_owner.tick(case.ctx, TickReport())
    assert called == ["batch"] and not case.checks and not case.events
    assert len(order_publications.pending(case.store)) == 1


@pytest.mark.parametrize("reason", ["Tests passed on current main", "Collection failed", "Missing dependency"])
def test_nonred_main_returns_to_preparation_with_reason_and_one_pm_request(case, reason):
    submit(case)
    case.git.remote.head = "main-2"
    case.verdict.update(status="hold", reason=reason)
    report = TickReport()
    assert order_publications.apply_next(case.ctx, report)
    row, = order_publications.requests(case.store)
    assert row["status"] == "preparation" and reason in row["reason"]
    rows = order_prepare.prepare(case.plan, case.root, case.config, case.git, case.store, status=True)
    assert rows[0]["status"] == "hold" and rows[0]["reason"] == reason
    assert len(orch_inbox.requests(case.store)) == 1
    assert not order_publications.apply_next(case.ctx, report)
    assert len(orch_inbox.requests(case.store)) == 1
    assert not case.events and case.git.remote.head == "main-2"


def test_owner_off_keeps_direct_publication(case):
    case.config = replace(case.config, merge_owner=False)
    report = submit(case)
    assert report.publication_request is None
    assert not order_publications.requests(case.store) and not case.checks
    assert [event[0] for event in case.events].count("push") == 1
    assert (case.root / NAME).exists()


def test_lost_push_response_recovers_issue_effects_without_second_publication(case):
    submit(case)
    case.git.remote.lost = True
    report = TickReport()
    assert order_publications.apply_next(case.ctx, report)
    row, = order_publications.pending(case.store)
    assert row["status"] == "applying" and not case.tracker.list_issues()
    assert order_publications.apply_next(case.ctx, report)
    row, = order_publications.requests(case.store)
    assert row["status"] == "published" and len(case.tracker.list_issues()) == 2
    assert len(case.checks) == 1
    assert len([event for event in case.events if event[0] == "push"]) == 1


def test_duplicate_submission_and_pending_views(case):
    first = submit(case)
    assert submit(case).publication_request == first.publication_request
    assert len(order_publications.requests(case.store)) == 1
    pending = status(case.store, case.config, case.ctx.now())["pending_publications"]
    assert pending[0]["id"] == first.publication_request
    assert brief.collect(case.store, case.config, case.ctx.now())["pending_publications"] == pending


@pytest.mark.parametrize("change", ["flags", "plan"])
def test_changed_request_replaces_only_its_orders_pending_request(case, change):
    first = submit(case)
    original, = order_publications.requests(case.store)
    other = deepcopy(original)
    other.update(id="other-order", sequence=original["sequence"] + 1)
    other["plan"]["id"] = "another-plan"
    order_publications._save(case.store, other)
    if change == "plan":
        case.plan["summary"] = "Revised order text"
        review_plan(case)
    second = submit(case, **({"approve": False, "urgent": True} if change == "flags" else {}))
    old, unrelated, new = order_publications.requests(case.store)
    assert second.publication_request != first.publication_request
    assert old["status"] == "superseded"
    assert new["id"] in old["reason"] and case.plan["id"] in old["reason"]
    assert unrelated == other and new["status"] == "pending"
    assert new["plan"] == case.plan and new["approve"] == (change != "flags")
    assert [row["id"] for row in order_publications.pending(case.store)] == [other["id"], new["id"]]
    assert new["sequence"] > unrelated["sequence"]
    assert not case.events and not case.checks


def test_only_replacement_is_published(case):
    first = submit(case, approve=False)
    second = submit(case, urgent=True)
    order_publications.apply_next(case.ctx, TickReport())
    old, new = order_publications.requests(case.store)
    assert old["id"] == first.publication_request and old["status"] == "superseded"
    assert new["id"] == second.publication_request and new["status"] == "published"
    assert len(case.checks) == 1
    assert len([event for event in case.events if event[0] == "push"]) == 1
    child, = [issue for issue in case.tracker.list_issues() if order._child_marker("publication", "new") in issue.body]
    assert "approved" in child.labels


def test_owner_skips_request_superseded_after_loading_the_pass(case, monkeypatch):
    first = submit(case)
    original, = order_publications.requests(case.store)
    earlier = deepcopy(original)
    earlier.update(id="earlier-order", sequence=original["sequence"] - 1)
    earlier["plan"]["id"] = "earlier-plan"
    order_publications._save(case.store, earlier)
    apply = order_publications._apply

    def replace_during_earlier_request(ctx, report, row):
        if row["id"] != earlier["id"]:
            return apply(ctx, report, row)
        case.plan["issues"][0]["acceptance_files"][NAME] = "def test_replacement(): pass\n"
        path = order_prepare._record_path(case.store, case.plan)
        record = order_prepare._read(path, case.plan)
        record["issues"]["new"]["written"] = deepcopy(case.plan["issues"][0]["acceptance_files"])
        order_prepare._write(path, record)
        review_plan(case)
        submit(case)
        row["status"] = "published"
        order_publications._save(case.store, row)

    monkeypatch.setattr(order_publications, "_apply", replace_during_earlier_request)
    assert order_publications.apply_next(case.ctx, TickReport())
    _, old, replacement = order_publications.requests(case.store)
    assert old["id"] == first.publication_request and old["status"] == "superseded"
    assert replacement["status"] == "pending"
    assert not case.events and not case.checks
    assert order_publications.apply_next(case.ctx, TickReport())
    _, old, published = order_publications.requests(case.store)
    assert old["status"] == "superseded" and published["status"] == "published"
    assert case.git.remote.snapshots[case.git.remote.head][NAME] == "def test_replacement(): pass\n"
    assert len([event for event in case.events if event[0] == "push"]) == 1


def test_published_unchanged_plan_returns_its_row_despite_changed_flags(case, monkeypatch):
    first = submit(case)
    order_publications.apply_next(case.ctx, TickReport())
    published, = order_publications.requests(case.store)
    wakes = []
    monkeypatch.setattr(order, "kick_tick", wakes.append)
    publication = order._publication_checks(case.plan, case.root, case.config, case.tracker,
                                           repo=case.config.repo, git=case.git, queued=True)
    returned = order_publications.enqueue(deepcopy(case.plan), case.root, case.config, case.config.repo,
                                         publication, approve=False, approve_keys=["new"], approve_numbers=[])
    second = submit(case, approve=False, urgent=True)
    assert returned == published
    assert second.publication_request == first.publication_request
    assert order_publications.requests(case.store) == [published]
    assert not order_publications.pending(case.store) and not order_publications.needs_owner(case.store)
    assert not order_publications.apply_next(case.ctx, TickReport())
    assert len(case.checks) == 1 and not wakes
    assert len([event for event in case.events if event[0] == "push"]) == 1


def test_older_published_plan_does_not_hide_a_newer_pending_request(case):
    original = deepcopy(case.plan)
    first = submit(case)
    order_publications.apply_next(case.ctx, TickReport())
    case.plan["summary"] = "Revised order text"
    review_plan(case)
    second = submit(case)
    case.plan = original
    review_plan(case)
    third = submit(case)
    rows = order_publications.requests(case.store)
    assert [row["status"] for row in rows] == ["published", "superseded", "pending"]
    assert third.publication_request not in {first.publication_request, second.publication_request}
    assert [row["id"] for row in order_publications.pending(case.store)] == [third.publication_request]


def test_duplicate_submission_supersedes_legacy_pending_requests(case):
    first = submit(case)
    latest, = order_publications.requests(case.store)
    old = deepcopy(latest)
    old.update(id="legacy-pending", sequence=latest["sequence"] - 1, fingerprint="older-payload", approve=False)
    order_publications._save(case.store, old)
    assert submit(case).publication_request == first.publication_request
    older, current = order_publications.requests(case.store)
    assert older["status"] == "superseded" and current["id"] in older["reason"]
    assert order_publications.pending(case.store) == [latest]


def test_replacement_keeps_an_applying_requests_push_recovery(case):
    submit(case)
    case.git.remote.lost = True
    order_publications.apply_next(case.ctx, TickReport())
    applying, = order_publications.pending(case.store)
    assert applying["status"] == "applying" and applying["commit"] == case.git.remote.head
    second = submit(case, approve=False)
    original, replacement = order_publications.requests(case.store)
    assert original == applying
    assert replacement["id"] == second.publication_request and replacement["status"] == "pending"


def test_one_request_per_owner_pass(case, monkeypatch):
    submit(case)
    first, = order_publications.pending(case.store)
    for index, name in enumerate(("second", "third"), 2):
        row = deepcopy(first)
        row.update(id=name, sequence=index, title=name)
        row["plan"].update(id=name, title=name)
        path = f"tests/acceptance/test_{name}.py"
        item = row["plan"]["issues"][0]
        item.update(acceptance_files={path: "def test_new(): pass\n"}, done_when=[f"uv run pytest {path}"])
        row["files"] = order.validate_plan(row["plan"], case.root, case.config)
        entry = row["prepared"]["new"]
        entry.update(written=item["acceptance_files"], done_when=item["done_when"],
                     cycle=order_prepare_cycle.identity(row["plan"], item, case.config, case.root, row["files"]),
                     published=order_prepare_setup.published_digest(row["plan"], case.root, case.config, row["files"]))
        order_publications._save(case.store, row)
    review = order_prepare_review.review

    def checked(plan, *args, **kwargs):
        case.verdict.update(status="hold" if plan["id"] == "second" else "reviewed",
                            reason="Tests passed on current main" if plan["id"] == "second" else "Still red")
        review(plan, *args, **kwargs)

    prepared = []
    monkeypatch.setattr(order_prepare_review, "review", checked)
    case.ctx.config = replace(case.config, merge_batch=True)
    monkeypatch.setattr("foragent.merge_owner_batch.prepare", lambda *args: prepared.append(
        [row["status"] for row in order_publications.requests(case.store)]))
    merge_owner.tick(case.ctx, TickReport())
    rows = order_publications.requests(case.store)
    assert [r["status"] for r in rows] == ["published", "preparation", "published"], [r["reason"] for r in rows]
    assert [check[0] for check in case.checks] == ["main-1", rows[0]["commit"], rows[0]["commit"]]
    assert case.git.remote.parents[rows[2]["commit"]] == rows[0]["commit"]
    assert len([event for event in case.events if event[0] == "push"]) == 2
    assert len(orch_inbox.requests(case.store)) == 1
    assert prepared == [["published", "preparation", "published"]]


def test_real_owner_tick_updates_observed_issues_and_approval(case):
    submit(case)
    merge_owner.tick(case.ctx, TickReport())
    row, = order_publications.requests(case.store)
    assert row["status"] == "published"
    child, = [issue for issue in case.tracker.list_issues() if order._child_marker("publication", "new") in issue.body]
    assert "approved" in child.labels
    observed, = [row for row in case.store.local_issues() if row["number"] == child.number]
    assert "approved" in observed["labels"]


def test_push_rejected_before_landing_rechecks_on_next_main(case):
    submit(case)
    case.git.remote.reject = True
    order_publications.apply_next(case.ctx, TickReport())
    assert len(order_publications.pending(case.store)) == 1
    assert not case.tracker.list_issues()
    case.git.remote.reject = False
    case.git.remote.head = "main-2"
    order_publications.apply_next(case.ctx, TickReport())
    assert [check[0] for check in case.checks] == ["main-1", "main-2"]
    assert order_publications.requests(case.store)[0]["status"] == "published"


def test_pending_request_wakes_owner_without_merge_admissions(case, monkeypatch):
    submit(case)
    launches = []
    monkeypatch.setattr("foragent.merge_owner.subprocess.Popen", lambda *args, **kw: launches.append(args))
    merge_owner.start(case.ctx, TickReport())
    assert len(launches) == 1
    order_publications.apply_next(case.ctx, TickReport())
    merge_owner.start(case.ctx, TickReport())
    assert len(launches) == 1


def test_collection_refusal_is_visible_in_preparation_status(case, monkeypatch):
    submit(case)

    def unavailable(*args, **kwargs):
        raise order.PlanError("Cannot collect on current main")

    monkeypatch.setattr(order_collect, "check", unavailable)
    order_publications.apply_next(case.ctx, TickReport())
    rows = order_prepare.prepare(case.plan, case.root, case.config, case.git, case.store, status=True)
    assert rows[0]["status"] == "interrupted"
    assert "Cannot collect" in rows[0]["reason"]
    assert not case.events


def test_request_keeps_admitted_evidence_after_original_record_disappears(case):
    submit(case)
    order_prepare._record_path(case.store, case.plan).unlink()
    case.plan["issues"][0]["acceptance_files"][NAME] = "changed after submission"
    order_publications.apply_next(case.ctx, TickReport())
    assert order_publications.requests(case.store)[0]["status"] == "published"
    assert case.checks[0][1][NAME] == "def test_new(): pass\n"


def test_implicit_legacy_tests_and_approval_selection_survive_caller_file_removal(case):
    item = case.plan["issues"][0]
    item.pop("statements")
    data = item.pop("acceptance_files")[NAME]
    path = case.root / NAME
    path.parent.mkdir(parents=True)
    path.write_text(data)
    # This is a separate legacy order, so the statement-route claim stays intact.
    case.plan["id"] = "legacy"
    report = submit(case, approve=False, approve_keys=["new"])
    row, = order_publications.pending(case.store)
    assert not row["approve"] and row["approve_keys"] == ["new"] and row["files"][NAME] == data
    path.unlink()
    order_publications.apply_next(case.ctx, TickReport())
    assert order_publications.requests(case.store)[0]["status"] == "published"
    assert case.checks[0][1][NAME] == data
    assert report.publication_request == row["id"]


def test_owner_reproduces_local_lane_skills_bound_by_preparation(case):
    case.config = replace(case.config, lanes={"api": {
        "paths": ["src/**"], "agent": "fa-api", "skills": ["local"]}})
    case.ctx.config = case.config
    item = case.plan["issues"][0]
    item["lane"] = "api"
    agent = case.root / ".claude/agents/fa-api.md"
    agent.parent.mkdir(parents=True)
    agent.write_text("Use the local skill.\n")
    skill = case.root / ".claude/skills/local/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: local\ndescription: Local conventions\n---\nUse public behaviour.\n")
    path = order_prepare._record_path(case.store, case.plan)
    record = order_prepare._read(path, case.plan)
    record["issues"]["new"]["cycle"] = order_prepare_cycle.identity(case.plan, item, case.config, case.root)
    order_prepare._write(path, record)
    submit(case)
    order_publications.apply_next(case.ctx, TickReport())
    row, = order_publications.requests(case.store)
    assert row["status"] == "published", row["reason"]


def test_restart_after_issue_writes_reuses_existing_publication(case, monkeypatch):
    submit(case)
    publish_issues = order._publish_issues

    def interrupted(*args, **kwargs):
        publish_issues(*args, **kwargs)
        raise GitError("Owner stopped before saving completion")

    monkeypatch.setattr(order, "_publish_issues", interrupted)
    order_publications.apply_next(case.ctx, TickReport())
    assert len(case.tracker.list_issues()) == 2
    assert len(order_publications.pending(case.store)) == 1
    monkeypatch.setattr(order, "_publish_issues", publish_issues)
    order_publications.apply_next(case.ctx, TickReport())
    assert order_publications.requests(case.store)[0]["status"] == "published"
    assert len(case.tracker.list_issues()) == 2 and len(case.checks) == 1
    assert len([event for event in case.events if event[0] == "push"]) == 1


def test_explicit_republication_gets_a_new_request_after_completion(case):
    original = deepcopy(case.plan)
    first = submit(case)
    order_publications.apply_next(case.ctx, TickReport())
    case.plan["summary"] = "Revised order text"
    review_plan(case)
    second = submit(case)
    assert second.publication_request != first.publication_request
    assert len(order_publications.pending(case.store)) == 1
    order_publications.apply_next(case.ctx, TickReport())
    assert len(case.tracker.list_issues()) == 2
    assert all(row["status"] == "published" for row in order_publications.requests(case.store))
    case.plan = original
    review_plan(case)
    third = submit(case)
    assert third.publication_request not in {first.publication_request, second.publication_request}
    assert len(order_publications.pending(case.store)) == 1


def test_publication_wakes_gate_from_persistent_root(case, monkeypatch):
    wakes = []
    monkeypatch.setattr(order, "kick_tick", wakes.append)
    submit(case)
    order_publications.apply_next(case.ctx, TickReport())
    assert wakes == [case.root, case.root]


def test_owner_keeps_the_existing_proposed_lane_publication_path(case):
    settings = "repo: owner/repo\nmerge_owner: true\ntest_command: uv run pytest\n"
    (case.root / ".foragent.yml").write_text(settings)
    case.git.remote.snapshots["main-1"] = {".foragent.yml": settings}
    case.plan["new_lanes"] = [{"name": "api", "paths": ["api/**"], "skills": []}]
    path = order_prepare._record_path(case.store, case.plan)
    record = order_prepare._read(path, case.plan)
    record["issues"]["new"]["cycle"] = order_prepare_cycle.identity(
        case.plan, case.plan["issues"][0], case.config, case.root)
    order_prepare._write(path, record)
    submit(case)
    order_publications.apply_next(case.ctx, TickReport())
    row, = order_publications.requests(case.store)
    assert row["status"] == "published", row["reason"]
    assert ".claude/agents/fa-api.md" in case.git.remote.snapshots[row["commit"]]
