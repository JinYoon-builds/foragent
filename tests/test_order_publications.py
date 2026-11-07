"""Off-owner red facts and owner recovery with fake Git, measured tests and GitHub."""

from copy import deepcopy
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
import shutil
import threading
from types import SimpleNamespace

import pytest

from test_order import PublishingGit, PublishingTracker, offline_publication  # noqa: F401
from foragent import brief, capacity, merge_owner, order, order_collect, order_prepare, order_prepare_cycle
from foragent import order_prepare_check, order_prepare_review, order_prepare_setup, order_publications, order_reviewed, orch_inbox
from foragent.config import Config
from foragent.git import GitError
from foragent.scheduler import TickReport
from foragent.state import StateStore
from foragent.status import collect as status
from foragent.tracker import TrackerError

NAME = "tests/acceptance/test_new.py"
COMMAND = f"uv run pytest {NAME}"
NODE = f"{NAME}::test_new"


class OwnerGit(PublishingGit):
    def __init__(self, root, events, remote=None, head="main-1"):
        super().__init__(root, events)
        self.head = head
        self.remote = remote or SimpleNamespace(head=head, batch=False, lost=False, reject=False,
                                                parents={}, snapshots={}, starts={}, titles={})

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
        self.remote.titles[self.head] = title
        self.remote.snapshots[self.head] = dict(self.saved)

    def is_ancestor(self, ancestor, head):
        ancestor = self.head if ancestor == "HEAD" else self.remote.head if ancestor == "origin/main" else ancestor
        head = self.head if head == "HEAD" else self.remote.head if head == "origin/main" else head
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
        if args == ["rev-parse", "--path-format=absolute", "--git-common-dir"]:
            return str(self.root / ".git")
        if args[0] == "ls-remote":
            return "batch refs/heads/foragent/batch-merge-open" if self.remote.batch else ""
        if args[0] == "remote":
            return "fake-origin"
        if args[0] == "log":
            title = next(arg.removeprefix("--grep=") for arg in args if arg.startswith("--grep="))
            return "\n".join(f"{sha}\0{subject}" for sha, subject in self.remote.titles.items()
                             if subject == title and self.is_ancestor(sha, args[-1]))
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

    def measured(git, base, context, place, verify, commands, directory, timeout_s, *, collect_node_ids=False):
        assert collect_node_ids
        tree = directory / "measured"
        tree.mkdir(parents=True, exist_ok=True)
        place(tree)
        written = {path.relative_to(tree).as_posix(): path.read_text()
                   for path in tree.rglob("*.py")}
        files = {name: data.decode() if isinstance(data, bytes) else data
                 for name, data in context.files.items()}
        checks.append((base, written, list(commands), files))
        results = []
        for command in commands:
            names = [part for part in command.split() if part.endswith(".py")]
            nodes = [f"{name}::test_new" for name in names]
            failed = verdict.get("failing_node_ids", nodes if verdict["status"] == "reviewed" else [])
            collected = verdict.get("collected_node_ids", nodes)
            results.append({"command": command, "exit_code": verdict.get("exit_code", 1 if failed else 0),
                            "timed_out": verdict.get("timed_out", False), "output": verdict["reason"],
                            "changed": verdict.get("changed", []), "failing_node_ids": failed,
                            "collected_node_ids": collected,
                            "nonred_node_ids": sorted(set(collected or []) - set(failed or []))})
        return results

    monkeypatch.setattr(order_prepare_check, "run", measured)
    monkeypatch.setattr(order_prepare_review, "review", lambda *args, **kwargs: pytest.fail("Publication ran an LLM review"))
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
    order_publications.check_pending(case.ctx, report)
    monkeypatch.setattr(order_prepare_check, "run", lambda *a, **kw: pytest.fail("Owner ran tests"))
    monkeypatch.setattr(order_collect, "check", lambda *a, **kw: pytest.fail("Owner ran collection"))
    merge_owner.tick(case.ctx, report)
    row, = order_publications.requests(case.store)
    assert row["status"] == "published"
    assert continued == [["published"]]
    assert [check[0] for check in case.checks] == ["main-1", "main-2"]
    assert case.checks[-1][:3] == ("main-2", {NAME: "def test_new(): pass\n"}, [COMMAND])
    assert case.git.remote.head == row["commit"]
    assert len(case.tracker.list_issues()) == 2
    assert len([event for event in case.events if event[0] == "push"]) == 1
    assert not order_publications.apply_next(case.ctx, report)
    assert len(case.checks) == 2  # Reviewed base seeded once, then the target measured.
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
    case.verdict.update({
        "Tests passed on current main": {"exit_code": 0},
        "Collection failed": {"exit_code": 2, "collected_node_ids": None, "failing_node_ids": None},
        "Missing dependency": {"exit_code": 127, "collected_node_ids": None, "failing_node_ids": None},
    }[reason])
    report = TickReport()
    order_publications.check_pending(case.ctx, report)
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
    order_publications.check_pending(case.ctx, TickReport())
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
    order_publications.check_pending(case.ctx, TickReport())
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
    for row in (earlier, original):
        row["red_check_commit"] = "main-1"
        order_publications._save(case.store, row)
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
    order_publications.check_pending(case.ctx, TickReport())
    assert order_publications.apply_next(case.ctx, TickReport())
    _, old, published = order_publications.requests(case.store)
    assert old["status"] == "superseded" and published["status"] == "published"
    assert case.git.remote.snapshots[case.git.remote.head][NAME] == "def test_replacement(): pass\n"
    assert len([event for event in case.events if event[0] == "push"]) == 1


def test_published_unchanged_plan_returns_its_row_despite_changed_flags(case, monkeypatch):
    first = submit(case)
    order_publications.check_pending(case.ctx, TickReport())
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
    order_publications.check_pending(case.ctx, TickReport())
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
    order_publications.check_pending(case.ctx, TickReport())
    case.git.remote.lost = True
    order_publications.apply_next(case.ctx, TickReport())
    applying, = order_publications.pending(case.store)
    assert applying["status"] == "applying" and applying["commit"] == case.git.remote.head
    second = submit(case, approve=False)
    original, replacement = order_publications.requests(case.store)
    assert original == applying
    assert replacement["id"] == second.publication_request and replacement["status"] == "pending"


def three_requests(case):
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
    return order_publications.pending(case.store)


def test_one_request_per_owner_pass(case, monkeypatch):
    rows = three_requests(case)
    # Seed the independent checker's durable outcomes before the owner tick.
    # The middle request became green; the other two retain fresh red evidence.
    for row in rows:
        fresh = row["id"] != "second"
        reason = "Still red" if fresh else "Tests passed on current main"
        admitted = row["prepared"]["new"]
        node = next(iter(admitted["written"])) + "::test_new"
        checked = deepcopy(admitted) | {
            "status": "reviewed" if fresh else "hold", "phase": "reviewing_tests" if fresh else "writing_tests",
            "reason": None if fresh else reason,
            "failing_node_ids": [node] if fresh else [], "collected_node_ids": [node],
            "measured": [{"command": admitted["done_when"][0], "exit_code": 1 if fresh else 0,
                          "timed_out": False, "output": reason, "changed": [],
                          "failing_node_ids": [node] if fresh else [], "collected_node_ids": [node],
                          "nonred_node_ids": [] if fresh else [node]}],
        }
        row["red_checks"] = {"new": checked}
        row["baseline_failing_node_ids"] = {"new": [node]}
        if fresh:
            row["red_check_commit"] = "main-1"
        else:
            row.update(status="preparation", reason=f"new: {reason}")
        order_publications._save(case.store, row)
    prepared = []
    case.ctx.config = replace(case.config, merge_batch=True)
    monkeypatch.setattr("foragent.merge_owner_batch.prepare", lambda *args: prepared.append(
        [row["status"] for row in order_publications.requests(case.store)]))

    order_publications._certify_pending(case.ctx, TickReport(), "main-1")
    case.checks.clear()
    merge_owner.tick(case.ctx, TickReport())

    rows = order_publications.requests(case.store)
    assert [r["status"] for r in rows] == ["published", "preparation", "published"], [r["reason"] for r in rows]
    assert not case.checks
    assert rows[0]["commit"] == rows[2]["commit"]
    assert case.git.remote.parents[rows[0]["commit"]] == "main-1"
    assert len([event for event in case.events if event[0] == "commit"]) == 1
    assert len([event for event in case.events if event[0] == "push"]) == 1
    assert len(case.tracker.list_issues()) == 4
    assert len(orch_inbox.requests(case.store)) == 1
    assert prepared == [["published", "preparation", "published"]]


def test_all_fresh_requests_land_in_one_commit_and_push(case, monkeypatch):
    three_requests(case)
    prepared = []
    case.ctx.config = replace(case.config, merge_batch=True)
    monkeypatch.setattr("foragent.merge_owner_batch.prepare", lambda *args: prepared.append(
        [row["status"] for row in order_publications.requests(case.store)]))
    order_publications.check_pending(case.ctx, TickReport())
    merge_owner.tick(case.ctx, TickReport())
    rows = order_publications.requests(case.store)
    assert [r["status"] for r in rows] == ["published"] * 3, [r["reason"] for r in rows]
    assert [check[0] for check in case.checks] == ["main-1"] * 6
    assert len([event for event in case.events if event[0] == "push"]) == 1
    assert len([event for event in case.events if event[0] == "commit"]) == 1
    assert len(case.tracker.list_issues()) == 6
    assert next(i for i, event in enumerate(case.events) if event[0] == "issue") > next(
        i for i, event in enumerate(case.events) if event[0] == "push")
    assert len({row["commit"] for row in rows}) == 1
    assert prepared == [["published"] * 3]
    order_publications.check_pending(case.ctx, TickReport())
    merge_owner.tick(case.ctx, TickReport())
    order_publications.check_pending(case.ctx, TickReport())
    merge_owner.tick(case.ctx, TickReport())
    rows = order_publications.requests(case.store)
    assert [r["status"] for r in rows] == ["published"] * 3
    assert len([event for event in case.events if event[0] == "push"]) == 1
    assert not orch_inbox.requests(case.store)


@pytest.mark.parametrize("after_first_issue", [False, True])
def test_batched_publication_recovers_after_push_without_repeated_issues(case, monkeypatch, after_first_issue):
    from foragent import order_flush_journal as journal

    class Interrupted(BaseException):
        pass

    three_requests(case)
    order_publications.check_pending(case.ctx, TickReport())
    publish_issues = order._publish_issues

    def interrupted(*args, **kwargs):
        if after_first_issue:
            publish_issues(*args, **kwargs)
        raise Interrupted()

    monkeypatch.setattr(order, "_publish_issues", interrupted)
    with pytest.raises(Interrupted):
        order_publications.apply_next(case.ctx, TickReport())
    assert len(case.tracker.list_issues()) == (2 if after_first_issue else 0)
    assert len([event for event in case.events if event[0] == "push"]) == 1
    assert not any(entry["kind"] == "done" for entry in journal.entries(case.store))
    case.ctx.store = StateStore(case.store.root)
    monkeypatch.setattr(order, "_publish_issues", publish_issues)
    monkeypatch.setattr(order_prepare_check, "run", lambda *a, **kw: pytest.fail("Recovery repeated tests"))
    monkeypatch.setattr(order_collect, "check", lambda *a, **kw: pytest.fail("Recovery repeated collection"))
    order_publications.apply_next(case.ctx, TickReport())
    order_publications.apply_next(case.ctx, TickReport())
    rows = order_publications.requests(case.store)
    assert [row["status"] for row in rows] == ["published"] * 3
    assert len(case.tracker.list_issues()) == 6
    assert len([event for event in case.events if event[0] == "issue"]) == 6
    assert len([event for event in case.events if event[0] == "commit"]) == 1
    assert len([event for event in case.events if event[0] == "push"]) == 1
    assert len([entry for entry in journal.entries(case.store) if entry["kind"] == "issues"]) == 3


def test_bad_member_is_reported_and_other_requests_land_together(case):
    three_requests(case)
    order_publications.check_pending(case.ctx, TickReport())
    rows = order_publications.pending(case.store)
    rows[1]["approve_keys"] = ["not-a-child"]
    order_publications._save(case.store, rows[1])
    report = TickReport()
    order_publications.apply_next(case.ctx, report)
    assert [row["status"] for row in order_publications.requests(case.store)] == ["pending", "preparation", "pending"]
    assert not case.events  # The remaining subset needs its own combined certificate.
    order_publications.check_pending(case.ctx, TickReport())
    order_publications.apply_next(case.ctx, report)
    rows = order_publications.requests(case.store)
    assert [row["status"] for row in rows] == ["published", "preparation", "published"]
    assert "unknown approve keys" in rows[1]["reason"]
    assert len(orch_inbox.requests(case.store)) == 1
    assert len(case.tracker.list_issues()) == 4
    assert len([event for event in case.events if event[0] == "commit"]) == 1
    assert len([event for event in case.events if event[0] == "push"]) == 1
    assert any(rows[1]["id"] in action and "preparation" in action for action in report.actions)
    assert not order_publications.apply_next(case.ctx, TickReport())


def test_stale_member_stays_pending_while_fresh_requests_land_together(case):
    three_requests(case)
    order_publications.check_pending(case.ctx, TickReport())
    rows = order_publications.pending(case.store)
    rows[1]["red_check_commit"] = "old-target"
    order_publications._save(case.store, rows[1])
    order_publications.apply_next(case.ctx, TickReport())
    assert not case.events  # A certificate for three cannot certify the fresh subset of two.
    order_publications._certify_pending(case.ctx, TickReport(), "main-1")
    order_publications.apply_next(case.ctx, TickReport())
    assert [row["status"] for row in order_publications.requests(case.store)] == ["published", "pending", "published"]
    assert len(case.tracker.list_issues()) == 4
    assert len([event for event in case.events if event[0] == "push"]) == 1


@pytest.mark.parametrize("failure", [order.PlanError, TrackerError])
def test_failed_issue_member_does_not_block_the_other_issues_on_recovery(case, monkeypatch, failure):
    three_requests(case)
    order_publications.check_pending(case.ctx, TickReport())
    publish_issues = order._publish_issues

    def fail_one(plan, *args, **kwargs):
        if plan["id"] == "second":
            raise failure("Issue write refused")
        return publish_issues(plan, *args, **kwargs)

    monkeypatch.setattr(order, "_publish_issues", fail_one)
    order_publications.apply_next(case.ctx, TickReport())
    assert [row["status"] for row in order_publications.requests(case.store)] == ["published", "applying", "published"]
    assert len(case.tracker.list_issues()) == 4
    monkeypatch.setattr(order, "_publish_issues", publish_issues)
    order_publications.apply_next(case.ctx, TickReport())
    assert [row["status"] for row in order_publications.requests(case.store)] == ["published"] * 3
    assert len(case.tracker.list_issues()) == 6
    assert len([event for event in case.events if event[0] == "push"]) == 1


def test_failed_github_preflight_is_reported_without_blocking_other_members(case, monkeypatch):
    three_requests(case)
    order_publications.check_pending(case.ctx, TickReport())
    check = order._publication_checks

    def denied(plan, *args, **kwargs):
        if plan["id"] == "second":
            raise TrackerError("Issue access denied")
        return check(plan, *args, **kwargs)

    monkeypatch.setattr(order, "_publication_checks", denied)
    order_publications.apply_next(case.ctx, TickReport())
    assert not case.events
    order_publications.check_pending(case.ctx, TickReport())
    order_publications.apply_next(case.ctx, TickReport())
    assert [row["status"] for row in order_publications.requests(case.store)] == ["published", "preparation", "published"]
    assert len(case.tracker.list_issues()) == 4
    assert len(orch_inbox.requests(case.store)) == 1
    assert len([event for event in case.events if event[0] == "push"]) == 1


def test_legacy_landed_intents_are_adopted_into_flush_issue_recovery(case):
    from foragent import order_flush_journal as journal

    three_requests(case)
    order_publications.check_pending(case.ctx, TickReport())
    case.git.remote.lost = True
    order_publications.apply_next(case.ctx, TickReport())
    # Earlier versions retained commit intents in requests without a flush journal.
    (case.store.root / journal.JOURNAL).write_text("")
    order_publications.apply_next(case.ctx, TickReport())
    assert [row["status"] for row in order_publications.requests(case.store)] == ["published"] * 3
    assert len(case.tracker.list_issues()) == 6
    assert len([event for event in case.events if event[0] == "push"]) == 1


def test_a_checker_lock_keeps_recovery_members_in_the_same_flush_batch(case):
    from foragent import order_flush_journal as journal

    three_requests(case)
    order_publications.check_pending(case.ctx, TickReport())
    case.git.remote.lost = True
    order_publications.apply_next(case.ctx, TickReport())
    second = order_publications.requests(case.store)[1]
    lock = case.store.lock_acceptance_baseline(f"order-publication-{second['id']}", blocking=False)
    try:
        order_publications.apply_next(case.ctx, TickReport())
        assert not case.tracker.list_issues()
        assert len([entry for entry in journal.entries(case.store) if entry["kind"] == "selected"]) == 1
    finally:
        lock.close()
    order_publications.apply_next(case.ctx, TickReport())
    assert len(case.tracker.list_issues()) == 6
    assert len([entry for entry in journal.entries(case.store) if entry["kind"] == "selected"]) == 1
    assert len([event for event in case.events if event[0] == "push"]) == 1


def test_first_request_context_failure_does_not_block_healthy_members(case, monkeypatch):
    three_requests(case)
    order_publications.check_pending(case.ctx, TickReport())
    checkout = order_publications._checkout

    @contextmanager
    def missing_context(ctx, target, row):
        if row["plan"].get("id") == "publication":
            raise order.PlanError("Could not reproduce preparation context")
        with checkout(ctx, target, row) as tree:
            yield tree

    monkeypatch.setattr(order_publications, "_checkout", missing_context)
    order_publications.apply_next(case.ctx, TickReport())
    assert not case.events
    order_publications.check_pending(case.ctx, TickReport())
    order_publications.apply_next(case.ctx, TickReport())
    assert [row["status"] for row in order_publications.requests(case.store)] == ["preparation", "published", "published"]
    assert len(case.tracker.list_issues()) == 4
    assert len([event for event in case.events if event[0] == "push"]) == 1
    assert len(orch_inbox.requests(case.store)) == 1


def test_shared_fetch_failure_keeps_requests_retryable_with_diagnostics(case, monkeypatch):
    three_requests(case)
    order_publications.check_pending(case.ctx, TickReport())

    def failed_fetch(*args):
        raise GitError("Fetch unavailable")

    with monkeypatch.context() as patch:
        patch.setattr(case.git, "fetch", failed_fetch)
        report = TickReport()
        order_publications.apply_next(case.ctx, report)
        assert len(report.actions) == 3
    assert all(row["reason"] == "Fetch unavailable" for row in order_publications.pending(case.store))
    order_publications.apply_next(case.ctx, TickReport())
    assert len(case.tracker.list_issues()) == 6
    assert len([event for event in case.events if event[0] == "push"]) == 1


def test_two_fresh_intents_for_one_order_reuse_issue_identities(case):
    submit(case)
    order_publications.check_pending(case.ctx, TickReport())
    case.git.remote.reject = True
    order_publications.apply_next(case.ctx, TickReport())
    case.git.remote.reject = False
    submit(case, approve=False)
    order_publications.check_pending(case.ctx, TickReport())
    pushes = len([event for event in case.events if event[0] == "push"])
    order_publications.apply_next(case.ctx, TickReport())
    assert [row["status"] for row in order_publications.requests(case.store)] == ["published", "published"]
    assert len(case.tracker.list_issues()) == 2
    assert len([event for event in case.events if event[0] == "issue"]) == 2
    assert len([event for event in case.events if event[0] == "push"]) == pushes + 1


@pytest.mark.parametrize("crash", [False, True])
def test_owner_batch_uses_real_git_history_for_one_push_and_recovery(case, offline_publication, monkeypatch, crash):
    from conftest import git_command

    root, _config, _tracker, git, calls, origin = offline_publication
    three_requests(case)
    case.ctx.git, case.ctx.repo_root = git, root
    before = git.head_sha()
    caller_files = git_command(root, "status", "--porcelain")
    order_publications.check_pending(case.ctx, TickReport())
    publish_issues = order._publish_issues
    if crash:
        class Interrupted(BaseException):
            pass

        def interrupted(*args, **kwargs):
            publish_issues(*args, **kwargs)
            raise Interrupted()

        monkeypatch.setattr(order, "_publish_issues", interrupted)
        with pytest.raises(Interrupted):
            order_publications.apply_next(case.ctx, TickReport())
        monkeypatch.setattr(order, "_publish_issues", publish_issues)
    order_publications.apply_next(case.ctx, TickReport())
    assert [row["status"] for row in order_publications.requests(case.store)] == ["published"] * 3
    assert len(case.tracker.list_issues()) == 6
    assert len([args for args in calls if "commit" in args]) == 1
    assert len([args for args in calls if args[0] == "push"]) == 1
    assert git_command(origin, "rev-list", "--count", f"{before}..main") == "1"
    for row in order_publications.requests(case.store):
        for name, data in row["files"].items():
            assert git_command(origin, "show", f"main:{name}") == data.rstrip("\n")
    assert git.head_sha() == before
    assert git_command(root, "status", "--porcelain") == caller_files


def test_real_owner_tick_updates_observed_issues_and_approval(case):
    submit(case)
    order_publications.check_pending(case.ctx, TickReport())
    merge_owner.tick(case.ctx, TickReport())
    row, = order_publications.requests(case.store)
    assert row["status"] == "published"
    child, = [issue for issue in case.tracker.list_issues() if order._child_marker("publication", "new") in issue.body]
    assert "approved" in child.labels
    observed, = [row for row in case.store.local_issues() if row["number"] == child.number]
    assert "approved" in observed["labels"]


def test_push_rejected_before_landing_rechecks_on_next_main(case):
    submit(case)
    order_publications.check_pending(case.ctx, TickReport())
    case.git.remote.reject = True
    order_publications.apply_next(case.ctx, TickReport())
    assert len(order_publications.pending(case.store)) == 1
    assert not case.tracker.list_issues()
    case.git.remote.reject = False
    case.git.remote.head = "main-2"
    order_publications.apply_next(case.ctx, TickReport())
    assert order_publications.requests(case.store)[0]["status"] == "applying"
    assert len(case.checks) == 1
    order_publications.check_pending(case.ctx, TickReport())
    order_publications.apply_next(case.ctx, TickReport())
    assert [check[0] for check in case.checks] == ["main-1", "main-2"]
    assert order_publications.requests(case.store)[0]["status"] == "published"


def test_pending_request_wakes_owner_without_merge_admissions(case, monkeypatch):
    submit(case)
    launches = []
    monkeypatch.setattr("foragent.merge_owner.subprocess.Popen", lambda *args, **kw: launches.append(args))
    merge_owner.start(case.ctx, TickReport())
    assert len(launches) == 1
    order_publications.check_pending(case.ctx, TickReport())
    order_publications.apply_next(case.ctx, TickReport())
    merge_owner.start(case.ctx, TickReport())
    assert len(launches) == 1


def test_collection_refusal_is_visible_in_preparation_status(case, monkeypatch):
    submit(case)

    def unavailable(*args, **kwargs):
        raise order.PlanError("Cannot collect on current main")

    monkeypatch.setattr(order_collect, "check", unavailable)
    order_publications.check_pending(case.ctx, TickReport())
    rows = order_prepare.prepare(case.plan, case.root, case.config, case.git, case.store, status=True)
    assert rows[0]["status"] == "interrupted"
    assert "Cannot collect" in rows[0]["reason"]
    assert not case.events


def test_request_keeps_admitted_evidence_after_original_record_disappears(case):
    submit(case)
    order_prepare._record_path(case.store, case.plan).unlink()
    case.plan["issues"][0]["acceptance_files"][NAME] = "changed after submission"
    order_publications.check_pending(case.ctx, TickReport())
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
    order_publications.check_pending(case.ctx, TickReport())
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
    order_publications.check_pending(case.ctx, TickReport())
    order_publications.apply_next(case.ctx, TickReport())
    row, = order_publications.requests(case.store)
    assert row["status"] == "published", row["reason"]


def test_restart_after_issue_writes_reuses_existing_publication(case, monkeypatch):
    submit(case)
    order_publications.check_pending(case.ctx, TickReport())
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
    order_publications.check_pending(case.ctx, TickReport())
    order_publications.apply_next(case.ctx, TickReport())
    case.plan["summary"] = "Revised order text"
    review_plan(case)
    second = submit(case)
    assert second.publication_request != first.publication_request
    assert len(order_publications.pending(case.store)) == 1
    order_publications.check_pending(case.ctx, TickReport())
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
    order_publications.check_pending(case.ctx, TickReport())
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
    order_publications.check_pending(case.ctx, TickReport())
    order_publications.apply_next(case.ctx, TickReport())
    row, = order_publications.requests(case.store)
    assert row["status"] == "published", row["reason"]
    assert ".claude/agents/fa-api.md" in case.git.remote.snapshots[row["commit"]]
    assert len([event for event in case.events if event[0] == "commit"]) == 1


def test_stale_publication_does_not_run_tests_or_delay_batches(case, monkeypatch):
    submit(case)
    order_publications.check_pending(case.ctx, TickReport())
    case.git.remote.head = "main-2"
    batches = []
    case.ctx.config = replace(case.config, merge_batch=True)
    monkeypatch.setattr("foragent.merge_owner_batch.prepare", lambda *args: batches.append("batch"))
    monkeypatch.setattr(order_prepare_check, "run", lambda *a, **kw: pytest.fail("Owner ran tests"))
    monkeypatch.setattr(order_collect, "check", lambda *a, **kw: pytest.fail("Owner ran collection"))
    merge_owner.tick(case.ctx, TickReport())
    row, = order_publications.pending(case.store)
    assert row["status"] == "pending" and row["red_check_commit"] == "main-1"
    assert batches == ["batch"] and not case.events


@pytest.mark.parametrize("neutral", [True, False])
def test_landing_reuses_only_neutral_intervening_commits(case, git_history, monkeypatch, neutral):
    from foragent import run_base_move
    from foragent.git import GitRepo

    first = git_history.tip
    case.git.remote.head = first
    submit(case)
    order_publications.check_pending(case.ctx, TickReport())
    # Two memory writes may land during a check. A result-affecting change that
    # is then reverted still invalidates evidence: inspect every commit.
    path = ".claude/agent-memory/api/MEMORY.md" if neutral else "src/app.py"
    git_history.update({path: "temporary\n"})
    middle = git_history.tip
    git_history.update({path: "latest\n" if neutral else None})
    target = git_history.tip
    real_git = GitRepo(git_history.clone)
    real_git.fetch("origin", "main")
    case.git.remote.parents.update({middle: first, target: middle})
    case.git.remote.head = target
    read = run_base_move._git_bytes
    monkeypatch.setattr(run_base_move, "_git_bytes", lambda git, args: read(real_git, args))
    monkeypatch.setattr(order_prepare_check, "run", lambda *a, **kw: pytest.fail("Owner repeated tests"))
    monkeypatch.setattr(order_collect, "check", lambda *a, **kw: pytest.fail("Owner repeated collection"))
    order_publications.apply_next(case.ctx, TickReport())
    row, = order_publications.requests(case.store)
    assert row["red_check_commit"] == first
    assert row["status"] == ("published" if neutral else "pending"), row["reason"]
    assert bool(case.events) is neutral


def test_off_owner_check_records_commit_and_exact_failing_node_ids(case):
    submit(case)
    case.git.remote.head = "main-2"
    order_publications.check_pending(case.ctx, TickReport())
    row, = order_publications.pending(case.store)
    assert row["red_check_commit"] == "main-2"
    assert row["red_checks"]["new"]["failing_node_ids"] == [NODE]
    assert row["baseline_failing_node_ids"]["new"] == [NODE]
    assert not case.events and not case.tracker.list_issues()
    order_publications.check_pending(case.ctx, TickReport())
    assert len(case.checks) == 2  # Neither the seeded base nor the target is measured again.


def test_legacy_added_node_proof_keeps_the_first_checked_base_when_main_moves(case, monkeypatch):
    item = case.plan["issues"][0]
    item.pop("statements")
    data = item.pop("acceptance_files")[NAME]
    path = case.root / NAME
    path.parent.mkdir(parents=True)
    path.write_text(data)
    case.plan["id"] = "legacy"
    submit(case)
    order_publications.check_pending(case.ctx, TickReport())
    first, = order_publications.pending(case.store)
    assert not first["prepared"] and first["baseline_red_check_commit"] == "main-1"
    bases = []
    monkeypatch.setattr(order_publications, "_plan_added_ids",
                        lambda git, base, *args: bases.append(base) or set())
    extra = f"{NAME}::test_main_only_parameter"
    case.git.remote.head = "main-2"
    case.verdict.update(failing_node_ids=[NODE, extra], collected_node_ids=[NODE, extra])
    order_publications.check_pending(case.ctx, TickReport())
    row, = order_publications.requests(case.store)
    assert bases == ["main-1"]
    assert row["status"] == "preparation" and extra in row["reason"]


def test_changed_failing_nodes_return_to_preparation_and_name_the_difference(case):
    submit(case)
    previous = f"{NAME}::test_previous"
    case.verdict.update(failing_node_ids=[NODE, previous], collected_node_ids=[NODE, previous])
    order_publications.check_pending(case.ctx, TickReport())
    other = "tests/test_existing.py::test_changed"
    case.git.remote.head = "main-2"
    case.verdict.update(failing_node_ids=[NODE, other], collected_node_ids=[NODE, other])
    order_publications.check_pending(case.ctx, TickReport())
    row, = order_publications.requests(case.store)
    assert row["status"] == "preparation"
    assert previous in row["reason"] and other in row["reason"]
    rows = order_prepare.prepare(case.plan, case.root, case.config, case.git, case.store, status=True)
    assert rows[0]["status"] in {"hold", "interrupted"}
    assert previous in rows[0]["reason"] and other in rows[0]["reason"]
    assert not case.events and len(orch_inbox.requests(case.store)) == 1


def test_restart_after_red_record_before_landing_does_not_double_publish(case, monkeypatch):
    submit(case)
    order_publications.check_pending(case.ctx, TickReport())
    case.ctx.store = StateStore(case.store.root)
    monkeypatch.setattr(order_prepare_check, "run", lambda *a, **kw: pytest.fail("Restart repeated tests"))
    order_publications.check_pending(case.ctx, TickReport())
    order_publications.apply_next(case.ctx, TickReport())
    order_publications.apply_next(case.ctx, TickReport())
    row, = order_publications.requests(case.store)
    assert row["status"] == "published"
    assert len(case.tracker.list_issues()) == 2
    assert len([event for event in case.events if event[0] == "push"]) == 1


def test_manual_pm_hold_is_not_checked_or_landed_and_owner_still_runs_batches(case, monkeypatch):
    submit(case)
    row, = order_publications.requests(case.store)
    row["status"] = "pm-held"
    order_publications._save(case.store, row)
    batches = []
    case.ctx.config = replace(case.config, merge_batch=True)
    monkeypatch.setattr("foragent.merge_owner_batch.prepare", lambda *args: batches.append("batch"))
    order_publications.check_pending(case.ctx, TickReport())
    merge_owner.tick(case.ctx, TickReport())
    assert not order_publications.pending(case.store) and not order_publications.needs_owner(case.store)
    assert order_publications.requests(case.store)[0]["status"] == "pm-held"
    assert batches == ["batch"] and not case.checks and not case.events


def test_owner_skips_a_request_while_its_checker_holds_the_request_lock(case, monkeypatch):
    submit(case)
    order_publications.check_pending(case.ctx, TickReport())
    row, = order_publications.requests(case.store)
    lock = case.store.lock_acceptance_baseline(f"order-publication-{row['id']}", blocking=False)
    assert lock is not None
    batches = []
    case.ctx.config = replace(case.config, merge_batch=True)
    monkeypatch.setattr("foragent.merge_owner_batch.prepare", lambda *args: batches.append("batch"))
    try:
        merge_owner.tick(case.ctx, TickReport())
    finally:
        lock.close()
    assert order_publications.requests(case.store)[0]["status"] == "pending"
    assert batches == ["batch"] and len(case.checks) == 1 and not case.events


def test_checker_result_does_not_resurrect_a_superseded_request(case, monkeypatch):
    first = submit(case)
    measure = order_prepare_check.run

    def superseded(*args, **kwargs):
        result = measure(*args, **kwargs)
        case.plan["summary"] = "Replacement while the checker was measuring"
        review_plan(case)
        submit(case)
        return result

    monkeypatch.setattr(order_prepare_check, "run", superseded)
    order_publications.check_pending(case.ctx, TickReport())
    old, new = order_publications.requests(case.store)
    assert old["id"] == first.publication_request and old["status"] == "superseded"
    assert new["status"] == "pending" and not new.get("red_check_commit")
    assert not case.events


def test_off_owner_collection_and_measurement_each_hold_a_test_slot(case, monkeypatch):
    submit(case)
    ownership = threading.local()
    measured = order_prepare_check.run
    calls = []

    @contextmanager
    def slot(configured, **kwargs):
        assert not getattr(ownership, "held", False)
        ownership.held = True
        try:
            yield
        finally:
            ownership.held = False

    def collect(*args, **kwargs):
        assert ownership.held
        calls.append("collection")
        return order_collect.Collected(args[4].head_sha(), 0)

    def measure(*args, **kwargs):
        assert ownership.held
        calls.append("measurement")
        return measured(*args, **kwargs)

    monkeypatch.setattr(capacity, "hold_test_slot", slot)
    monkeypatch.setattr(order_collect, "check", collect)
    monkeypatch.setattr(order_prepare_check, "run", measure)
    order_publications.check_pending(case.ctx, TickReport())
    row, = order_publications.pending(case.store)
    assert row["red_check_commit"] == "main-1"
    assert calls == ["collection", "measurement"]


def test_independent_publications_check_concurrently_with_two_test_slots(case, monkeypatch):
    submit(case)
    first, = order_publications.requests(case.store)
    second = deepcopy(first)
    second.update(id="second-request", sequence=first["sequence"] + 1)
    second["plan"]["id"] = "second-plan"
    item = second["plan"]["issues"][0]
    second["files"] = order.validate_plan(second["plan"], case.root, case.config)
    second["prepared"]["new"]["cycle"] = order_prepare_cycle.identity(
        second["plan"], item, case.config, case.root, second["files"])
    second["prepared"]["new"]["published"] = order_prepare_setup.published_digest(
        second["plan"], case.root, case.config, second["files"])
    order_publications._save(case.store, second)
    case.ctx.config = replace(case.config, test_slots=2)
    monkeypatch.setattr(capacity, "effective_test_slots", lambda *args, **kwargs: 2)
    ownership, barrier = threading.local(), threading.Barrier(2)
    semaphore, counts_lock = threading.BoundedSemaphore(2), threading.Lock()
    counts = {"active": 0, "maximum": 0}
    collection_threads, measurement_threads = set(), set()
    measured = order_prepare_check.run

    @contextmanager
    def slot(configured, **kwargs):
        assert configured == 2 and not getattr(ownership, "held", False)
        with semaphore:
            ownership.held = True
            with counts_lock:
                counts["active"] += 1
                counts["maximum"] = max(counts["maximum"], counts["active"])
            try:
                yield
            finally:
                with counts_lock:
                    counts["active"] -= 1
                ownership.held = False

    def collect(*args, **kwargs):
        assert ownership.held
        if threading.current_thread() is not threading.main_thread():  # the concurrent check pass
            collection_threads.add(threading.get_ident())
        return order_collect.Collected(args[4].head_sha(), 0)

    def measure(*args, **kwargs):
        assert ownership.held
        if threading.current_thread() is not threading.main_thread():  # the concurrent check pass
            measurement_threads.add(threading.get_ident())
            try:
                barrier.wait(timeout=5)
            except threading.BrokenBarrierError:
                pytest.fail(str([(row["status"], row["reason"])
                                 for row in order_publications.requests(case.store)]))
        return measured(*args, **kwargs)

    monkeypatch.setattr(capacity, "hold_test_slot", slot)
    monkeypatch.setattr(order_collect, "check", collect)
    monkeypatch.setattr(order_prepare_check, "run", measure)
    order_publications.check_pending(case.ctx, TickReport())
    rows = order_publications.pending(case.store)
    assert len(rows) == 2 and all(row["red_check_commit"] == "main-1" for row in rows)
    assert len(collection_threads) == len(measurement_threads) == 2
    assert collection_threads == measurement_threads and counts == {"active": 0, "maximum": 2}
    assert not case.events


@pytest.mark.parametrize("phase", ["collection", "measurement"])
def test_git_error_during_off_owner_check_defers_without_freshness(case, monkeypatch, phase):
    submit(case)
    original, = order_publications.requests(case.store)

    def unavailable(*args, **kwargs):
        raise GitError("Temporary checkout read failure")

    monkeypatch.setattr(order_collect if phase == "collection" else order_prepare_check,
                        "check" if phase == "collection" else "run", unavailable)
    report = TickReport()
    order_publications.check_pending(case.ctx, report)
    row, = order_publications.pending(case.store)
    assert row == original and not row.get("red_check_commit")
    assert any("deferred" in action and "Temporary checkout read failure" in action for action in report.actions)
    assert not orch_inbox.requests(case.store) and not case.events
