"""Real Git prefixes and measured CI, with the normal owner as the only merger."""

from contextlib import contextmanager
from dataclasses import replace
from datetime import timedelta
import json

import pytest

from helpers.merge_world import world  # noqa: F401
from conftest import git_command
from test_merge_owner_proto import proto  # noqa: F401
import test_merge_owner_proto as support
from foragent import merge_owner, merge_owner_batch as batch, orch_queue
from foragent.config import ConfigError, load_config
from foragent.git import GitError
from foragent.issue import body_hash
from foragent.state import StateStore
from foragent.tracker import GhTracker, RejectedEvidence, TrackerError


@pytest.fixture
def batched(proto, monkeypatch):
    case = proto
    case.config = replace(case.config, merge_batch=True)
    case.ctx.config = case.config
    case.pending_batch = False
    case.batch_pushes = []
    for key in ("GIT_AUTHOR_DATE", "GIT_COMMITTER_DATE"):
        monkeypatch.setenv(key, case.clock[0].isoformat())
    transport = case.git._run

    def git_transport(argv):
        args = list(argv)
        if args[:1] == ["-C"]:
            args = args[2:]
        if args[:1] == ["push"]:
            source, destination = args[-1].split(":", 1) if ":" in args[-1] else (args[-1], args[-1])
            assert destination not in {"main", "refs/heads/main"}, "Batch pushed the target"
            lease = next((a.split("=", 1)[1] for a in args if a.startswith("--force-with-lease=")), None)
            if lease is not None:
                ref, expected = lease.split(":", 1)
                actual = git_command(case.history.origin, "for-each-ref", "--format=%(objectname)", ref)
                if actual != expected:
                    raise GitError("lease rejected")
            if source and batch.PREFIX in destination:
                case.batch_pushes.append((source, destination))
        return transport(argv)

    monkeypatch.setattr(case.git, "_run", git_transport)
    create = case.tracker.create_pr

    def created(**kwargs):
        if kwargs["head"].startswith(batch.PREFIX) and any(
                pr.head == kwargs["head"] and pr.base == kwargs["base"] and case.tracker.get_issue(pr.number).state == "open"
                for pr in case.tracker.created_prs):
            raise TrackerError("A pull request already exists for this head/base")
        pr = create(**kwargs)
        if kwargs["head"].startswith(batch.PREFIX) and case.pending_batch:
            case.tracker.set_pr_checks(pr, "pending")
        return pr

    monkeypatch.setattr(case.tracker, "create_pr", created)
    reports = case.tracker.pr_acceptance_reports

    def measured(head):
        row = case.ci.by_head[head]
        return [] if row.branch.startswith(batch.PREFIX) and case.pending_batch else reports(head)

    monkeypatch.setattr(case.tracker, "pr_acceptance_reports", measured)
    return case


def queue_workers(case, numbers):
    support.queue_workers(case, numbers)
    if getattr(case, "pending_batch", False):
        for n in numbers:
            case.tracker.set_pr_checks(support.pr_for(case, n).number, "pending")


def groups(case):
    return [pr for pr in case.tracker.created_prs if pr.head.startswith(batch.PREFIX)]


def refs(case):
    return git_command(case.history.origin, "for-each-ref", "--format=%(refname)", f"refs/heads/{batch.PREFIX}*")


def events(case):
    return [row["data"] for row in case.store.flow_records() if row["kind"] == "merge-batch"]


def green(case):
    case.pending_batch = False
    for pr in case.tracker.created_prs:
        case.tracker.set_pr_checks(pr.number, "pass")


def shared_locks(case):
    # An open owner shares these locks: completing one member leaves the same
    # measurement scope, so the tests exercise tree reuse rather than scope refresh.
    approval = case.store.load_approval(5)
    paths = [f"tests/acceptance/test_own_{n}.py" for n in [5, 1, 4, 2]]
    body = approval.body.replace("tests/acceptance/test_own_5.py", " ".join(paths))
    case.clock[0] += timedelta(seconds=1)
    case.tracker.add_issue(79, "Shared acceptance owner", body, labels=["approved"])
    case.tracker.record_label(79, "approved", "human")
    blobs = {p: case.git.blob_sha(case.base, p) for p in paths}
    case.store.save_approval(79, body=body, body_sha=body_hash(body), acceptance_blobs=blobs,
                             approved_by="human", approved_at=case.clock[0].isoformat())


@pytest.mark.parametrize("numbers", [[5, 1], [2, 4, 1, 5]])
def test_every_prefix_published_before_single_path_merges_and_reused_across_restart(batched, monkeypatch, numbers):
    case = batched
    shared_locks(case)
    queue_workers(case, numbers)
    orch_queue.set_global(case.store, numbers)
    base = case.history.tip
    heads = {n: case.tracker.pr_head_sha(support.pr_for(case, n).number) for n in numbers}
    report = support.gate(case)
    assert case.store.load_approval(79) is not None, report.actions
    assert len(groups(case)) == len(numbers)
    assert len(case.merges) == 1  # Never a bulk landing.
    for k, pr in enumerate(groups(case), 1):
        assert pr.head == f"{batch.PREFIX}{base[:8]}-{k}"
        prefix = case.tracker.pr_head_sha(pr.number)
        decoded_base, chain = batch._decode(case.git, pr.head, prefix)
        assert decoded_base == base
        assert [(n, head) for n, head, _, _ in chain] == [(n, heads[n]) for n in numbers[:k]]
        assert git_command(case.root, "show", "-s", "--format=%cn <%ce>", prefix) == "foragent <foragent@localhost>"
    # Refreshed member CI never completes: only matching prefix reports can land it.
    checks, reports = case.tracker.pr_checks, case.tracker.pr_acceptance_reports

    def own_round_pending(pr):
        row = case.tracker.get_pr(pr)
        return "pending" if row.head.startswith("foragent/issue-") and row.head_sha not in heads.values() else checks(pr)

    def no_new_own_reports(head):
        row = case.ci.by_head[head]
        assert not (row.branch.startswith("foragent/issue-") and head not in heads.values())
        return reports(head)

    monkeypatch.setattr(case.tracker, "pr_checks", own_round_pending)
    monkeypatch.setattr(case.tracker, "pr_acceptance_reports", no_new_own_reports)
    # Each gate reconstructs its context/store from disk. No batch state file is needed.
    support.drain(case, count=len(numbers))
    assert not refs(case)
    assert len(case.merges) == len(numbers)
    assert case.merges[0]["head"] == heads[numbers[0]]
    assert all(m["head"] != heads[n] for n, m in zip(numbers[1:], case.merges[1:]))
    assert all(any(frame["function"] == "_advance" and frame["path"].endswith("/merge_owner.py")
                   for frame in merge["client"]) for merge in case.merges)
    assert all(not any(frame["path"].endswith("/merge_owner_batch.py") for frame in merge["client"])
               for merge in case.merges)
    assert events(case)[-1]["result"] == "used"
    assert events(case)[-1]["used_members"] == len(numbers)
    assert events(case)[-1]["ci_minutes"] == pytest.approx(2 * len(numbers) / 60)
    assert len(case.store.owner_merge_facts()) == len(numbers)
    turns = [row for row in merge_owner.queue._rows(case.store)
             if row.get("event") == "turn" and row.get("result") == "merged"]
    assert [row["base_sha"] for row in turns] == [
        git_command(case.history.origin, "rev-parse", f"{merge['main']}^1") for merge in case.merges]


def test_pending_prefixes_run_in_parallel_without_merges(batched):
    case = batched
    shared_locks(case)
    case.pending_batch = True
    queue_workers(case, [5, 1, 4, 2])
    support.gate(case)
    assert len(groups(case)) == 4 and len(refs(case).splitlines()) == 4
    assert not case.merges
    original = list(case.batch_pushes)
    support.gate(case)
    assert case.batch_pushes == original and not case.merges
    green(case)
    support.drain(case)
    assert len(case.merges) == 4 and not refs(case)


@pytest.mark.parametrize("configured,expected", [(None, 4), (6, 6)], ids=["default", "configured-six"])
def test_batch_size_limits_prefixes_and_survives_restart(request, monkeypatch, configured, expected):
    from helpers import merge_world as contract

    numbers = (5, 1, 4, 2, 6, 3, 7)
    monkeypatch.setattr(contract, "NUMBERS", numbers)
    case = request.getfixturevalue("batched")
    settings = case.root / ".foragent.yml"
    if configured is not None:
        settings.write_text(settings.read_text() + f"merge_batch_size: {configured}\n")
    case.config = replace(case.config, merge_batch_size=load_config(case.root).merge_batch_size)
    case.ctx.config = case.config
    assert case.config.merge_batch_size == expected
    case.pending_batch = True
    queue_workers(case, numbers)
    support.gate(case)
    assert len(groups(case)) == len(refs(case).splitlines()) == expected
    pushes = list(case.batch_pushes)
    support.gate(case)  # Rebuild the owner from disk and decode the longest prefix.
    assert len(groups(case)) == len(refs(case).splitlines()) == expected
    assert case.batch_pushes == pushes and not case.merges and not events(case)
    pr = groups(case)[-1]
    base, chain = batch._decode(case.git, pr.head, case.tracker.pr_head_sha(pr.number))
    assert base == case.base and [n for n, *_ in chain] == list(numbers[:expected])


def test_green_earlier_prefix_is_usable_when_later_prefix_is_red(batched, monkeypatch):
    case = batched
    shared_locks(case)
    queue_workers(case, [5, 1])
    reports = case.tracker.pr_acceptance_reports

    def red_tip(head):
        values = reports(head)
        if case.ci.by_head[head].branch.endswith("-2"):
            for value in values:
                value["report"]["head"]["passing_ids"] = []
        return values

    monkeypatch.setattr(case.tracker, "pr_acceptance_reports", red_tip)
    support.gate(case)
    assert case.tracker.get_issue(5).state == "closed" and refs(case)
    support.gate(case)  # Refresh still runs before consulting prefix CI.
    case.tracker.set_pr_checks(support.pr_for(case, 1).number, "pending")
    support.gate(case)
    assert not refs(case)
    assert events(case)[-1]["used_members"] == 1 and "regression" in events(case)[-1]["reason"]
    case.tracker.set_pr_checks(support.pr_for(case, 1).number, "pass")
    support.drain(case, count=2)
    assert len(groups(case)) == 2 and case.tracker.get_issue(1).state == "closed"


@pytest.mark.parametrize("change", ["approval", "blocked", "needs-human", "generation", "approval-generation", "closed", "ci", "head", "prefix-ci", "prefix-reports"])
def test_ci_green_cannot_bypass_any_fresh_locked_single_path_check(batched, monkeypatch, change):
    case = batched
    case.pending_batch = True
    queue_workers(case, [5, 1])
    support.gate(case)
    green(case)
    case.tracker.set_pr_checks(support.pr_for(case, 5).number, "pending")
    lock = StateStore.merge_lock
    changed = []

    @contextmanager
    def changed_under_lock(store):
        with lock(store):
            if not changed:
                changed.append(change)
                if change == "approval":
                    case.tracker.remove_labels(5, ["approved"])
                elif change in {"blocked", "needs-human"}:
                    case.tracker.add_labels(5, [change])
                elif change == "generation":
                    case.store.start_run(5, branch="replacement", worktree=case.store.root / "replacement")
                elif change == "approval-generation":
                    approval = case.store.load_approval(5)
                    case.store.save_approval(**replace(approval, approved_at=case.clock[0].isoformat()).__dict__)
                elif change == "closed":
                    case.tracker.close_issue(5)
                elif change == "ci":
                    case.tracker.set_pr_checks(support.pr_for(case, 5).number, "fail")
                elif change == "prefix-ci":
                    case.tracker.set_pr_checks(groups(case)[0].number, "fail")
                elif change == "prefix-reports":
                    case.pending_batch = True
                else:
                    support.update_pr_branch(case, 5)
            yield

    monkeypatch.setattr(StateStore, "merge_lock", changed_under_lock)
    support.gate(case)
    assert changed and not case.tracker.get_pr(support.pr_for(case, 5).number).merged_at
    assert not refs(case)
    assert events(case)[-1]["result"] == "unused"


def test_changed_scope_keeps_the_ordinary_refresh_requirement(batched):
    case = batched
    queue_workers(case, [5, 1])
    head = case.tracker.pr_head_sha(support.pr_for(case, 1).number)
    support.gate(case)
    support.gate(case)  # #5's acceptance tests are now required on main.
    case.tracker.set_pr_checks(support.pr_for(case, 1).number, "pending")
    support.gate(case)  # The refreshed head reaches the CI substitution boundary.
    assert not refs(case)
    assert not case.tracker.get_pr(support.pr_for(case, 1).number).merged_at
    assert case.tracker.pr_head_sha(support.pr_for(case, 1).number) != head
    case.tracker.set_pr_checks(support.pr_for(case, 1).number, "pass")
    support.drain(case, count=2)
    assert len(groups(case)) == 2


def test_cleanup_restart_keeps_the_retired_tip_and_never_reuses_evidence(batched, monkeypatch):
    case = batched
    shared_locks(case)
    queue_workers(case, [5, 1])
    support.gate(case)
    support.gate(case)  # Refresh the remaining member before its CI read/merge.
    transport = case.git._run
    failed = []

    def unavailable_once(argv):
        if argv[0] == "push" and argv[-1].startswith(":refs/heads/" + batch.PREFIX) and not failed:
            failed.append(True)
            raise GitError("cleanup unavailable")
        return transport(argv)

    monkeypatch.setattr(case.git, "_run", unavailable_once)
    support.gate(case)
    assert failed and refs(case) and not merge_owner.admissions(case.store)
    assert len(events(case)) == 1
    case.pending_batch = True
    support.gate(case)
    assert not refs(case) and len(events(case)) == 1 and len(case.merges) == 2


def test_retirement_cancels_owned_prefix_ci_before_deleting_refs_and_retries(batched, monkeypatch):
    case = batched
    case.pending_batch = True
    queue_workers(case, [5, 1])
    support.gate(case)
    runs, owned = [], []
    for pr in groups(case):
        head = case.tracker.pr_head_sha(pr.number)
        for status in ["queued", "in_progress", "completed"]:
            row = dict(id=len(runs) + 1, head_sha=head, head_branch=pr.head, status=status,
                       repository={"full_name": case.ctx.repo}, head_repository={"full_name": case.ctx.repo})
            runs.append(row)
            if status != "completed":
                owned.append(row["id"])
        for fields in [dict(head_branch="other"), dict(head_sha="other"),
                       dict(head_repository={"full_name": "fork/repo"}),
                       dict(repository={"full_name": "other/repo"})]:
            runs.append({**row, "id": len(runs) + 1, "status": "queued", **fields})
    cancelled, failed = [], []

    def actions_api(args, input=None):
        if args[:4] == ["api", "--paginate", "--jq", ".workflow_runs"]:
            assert "head_sha=" in args[-1] and "per_page=100" in args[-1]
            return json.dumps(runs[:3]) + "\n" + json.dumps(runs[3:])
        assert args[:3] == ["api", "-X", "POST"] and args[-1].endswith("/cancel")
        row = next(row for row in runs if str(row["id"]) == args[-1].split("/")[-2])
        assert row["id"] in owned
        assert case.git.remote_branch_sha(row["head_branch"]) == row["head_sha"]
        if row["status"] == "in_progress" and not failed:
            failed.append(True)
            raise TrackerError("cancellation unavailable")
        row["status"] = "completed"
        cancelled.append(row["id"])
        return ""

    monkeypatch.setattr(case.tracker, "cancel_workflow_runs",
                        GhTracker(case.ctx.repo, run=actions_api).cancel_workflow_runs)
    transport = case.git._run

    def delete_after_cancel(argv):
        if argv[0] == "push" and argv[-1].startswith(":refs/heads/" + batch.PREFIX):
            branch = argv[-1].removeprefix(":refs/heads/")
            assert all(row["status"] == "completed" for row in runs
                       if row["id"] in owned and row["head_branch"] == branch)
        return transport(argv)

    monkeypatch.setattr(case.git, "_run", delete_after_cancel)
    case.clock[0] += timedelta(seconds=batch.DEADLINE_SECONDS)
    support.gate(case)
    assert failed and refs(case) and cancelled == owned[:1]
    support.gate(case)
    assert not refs(case) and cancelled == owned
    assert all(case.tracker.get_pr(pr.number).state == "closed" for pr in groups(case))
    assert len(events(case)) == 1


def test_disabling_batching_retires_published_prefixes_idempotently(batched, monkeypatch):
    case = batched
    case.pending_batch = True
    queue_workers(case, [5, 1])
    support.gate(case)
    pushes = list(case.batch_pushes)
    case.config = replace(case.config, merge_batch=False)
    case.ctx.config = case.config
    monkeypatch.setattr(batch, "prepare", lambda *args: pytest.fail("Disabled batch prepared evidence"))
    transport = case.git._run
    failed = []

    def interrupted_delete(argv):
        if argv[0] == "push" and argv[-1].startswith(":refs/heads/" + batch.PREFIX) and not failed:
            failed.append(True)
            raise GitError("cleanup unavailable")
        return transport(argv)

    monkeypatch.setattr(case.git, "_run", interrupted_delete)
    support.gate(case)
    assert failed and refs(case) and len(events(case)) == 1
    green(case)
    support.drain(case, count=2)
    support.gate(case)
    assert not refs(case) and len(events(case)) == 1 and case.batch_pushes == pushes
    assert all(case.tracker.get_pr(pr.number).state == "closed" for pr in groups(case))
    assert len(case.merges) == 2 and events(case)[0]["used_members"] == 0


def test_invalid_tip_does_not_prevent_retirement_of_intact_prefixes(batched):
    case = batched
    case.pending_batch = True
    queue_workers(case, [5, 1])
    support.gate(case)
    intact, changed = groups(case)
    git_command(case.history.origin, "update-ref", f"refs/heads/{changed.head}", case.base)
    case.tracker.set_pr_head_sha(changed.number, case.base)
    support.gate(case)
    assert case.tracker.get_pr(intact.number).state == "closed"
    assert refs(case).splitlines() == [f"refs/heads/{changed.head}"]
    green(case)
    support.drain(case, count=2)
    support.gate(case)
    assert len(case.merges) == 2 and len(groups(case)) == 2 and len(events(case)) == 1
    assert refs(case).splitlines() == [f"refs/heads/{changed.head}"]
    assert case.tracker.get_pr(changed.number).state == "open"


def test_retargeted_prefix_is_retained_by_the_owner_close_guard(batched):
    case = batched
    shared_locks(case)
    case.pending_batch = True
    queue_workers(case, [5, 1])
    support.gate(case)
    pr = case.tracker.get_pr(groups(case)[0].number)
    git_command(case.history.origin, "update-ref", "refs/heads/release", case.base)
    case.tracker.update_pr(pr.number, base="release", title=pr.title, body=pr.body)
    with pytest.raises(TrackerError, match="PR identity changed"):
        merge_owner.close_for_repair(case.ctx, None, pr.number, pr.head_sha, "main", branch=pr.head)
    report = support.gate(case)
    assert any("PR identity changed" in action for action in report.actions)
    assert case.tracker.get_pr(pr.number).state == "open"
    assert case.git.remote_branch_sha(pr.head) == pr.head_sha
    support.gate(case)
    assert case.tracker.get_pr(pr.number).state == "open"
    assert case.git.remote_branch_sha(pr.head) == pr.head_sha


@pytest.mark.parametrize("interference", ["unrelated", "empty", "different-tree"])
def test_target_changes_still_require_refresh_and_only_identical_trees_reuse_ci(batched, monkeypatch, interference):
    case = batched
    shared_locks(case)
    queue_workers(case, [5, 1])
    head = case.tracker.pr_head_sha(support.pr_for(case, 1).number)
    if interference == "different-tree":
        merge = case.tracker.merge_pr

        def altered(*args, **kwargs):
            merge(*args, **kwargs)
            case.history.update({"src/member_5.py": "VALUE = 'different merge result'\n"})

        monkeypatch.setattr(case.tracker, "merge_pr", altered)
    support.gate(case)
    if interference != "different-tree":
        case.history.update({} if interference == "empty" else {"src/unrelated.py": "VALUE = 17\n"})
    support.gate(case)
    assert not case.tracker.get_pr(support.pr_for(case, 1).number).merged_at
    assert case.tracker.pr_head_sha(support.pr_for(case, 1).number) != head
    case.tracker.set_pr_checks(support.pr_for(case, 1).number, "pending")
    support.gate(case)
    assert not refs(case)
    if interference == "empty":
        assert len(case.merges) == 2  # Same trees remain reusable across an empty target commit.
    else:
        assert len(case.merges) == 1


def test_conflict_discards_entire_candidate_without_splitting_or_retry(batched, monkeypatch):
    case = batched
    model = case.ctx.executor_factory("claude")
    implement = model.run

    def incompatible(**kwargs):
        result = implement(**kwargs)
        if case.worker.number in {5, 4}:
            (kwargs["worktree"] / "src/shared.py").write_text(f"VALUE = {case.worker.number}\n")
        return result

    monkeypatch.setattr(model, "run", incompatible)
    queue_workers(case, [5, 1, 4, 2])
    support.gate(case)
    assert not groups(case) and not refs(case)
    assert events(case)[-1]["result"] == "unused"
    assert [m["n"] for m in events(case)[-1]["members"]] == [5, 1, 4, 2]
    support.drain(case)
    assert not groups(case)  # Even the healthy suffix never forms a retry batch.


@pytest.mark.parametrize("failure", ["deadline", "missing", "rejected", "outage"])
def test_unusable_candidate_stops_evidence_and_members_resume_single_path(batched, monkeypatch, failure):
    case = batched
    case.pending_batch = True
    queue_workers(case, [5, 1])
    support.gate(case)
    if failure == "deadline":
        case.clock[0] += timedelta(seconds=batch.DEADLINE_SECONDS)
        green(case)  # Ordinary member CI can complete while the prefix expires.
    else:
        green(case)
        reports = case.tracker.pr_acceptance_reports

        def broken(head):
            if case.ci.by_head[head].branch.startswith(batch.PREFIX):
                if failure == "missing":
                    return []
                raise RejectedEvidence("invalid CI") if failure == "rejected" else TrackerError("CI unavailable")
            return reports(head)

        monkeypatch.setattr(case.tracker, "pr_acceptance_reports", broken)
    support.drain(case, count=2)
    assert not refs(case) and len(groups(case)) == 2
    assert events(case)[-1]["result"] == "unused"
    assert all(case.tracker.get_issue(n).state == "closed" for n in [5, 1])


def test_merge_batch_false_uses_the_unchanged_single_path(proto, monkeypatch):
    case = proto
    monkeypatch.setattr(batch, "prepare", lambda *args: pytest.fail("Disabled batch was consulted"))
    queue_workers(case, [5, 1])
    support.drain(case, count=2)
    assert len(case.merges) == 2
    assert not groups(case) and not events(case)


@pytest.mark.parametrize("settings", ["merge_batch: yes\n", "merge_owner: true\nchecks: local\nmerge_batch: true\n", "merge_batch: 1\n"])
def test_merge_batch_requires_owner_ci_and_a_boolean(tmp_path, settings):
    (tmp_path / ".foragent.yml").write_text(settings)
    with pytest.raises(ConfigError, match="merge_batch"):
        load_config(tmp_path)


def update_base(case, files):
    case.history.update(files)
    git_command(case.root, "fetch", "origin")
    git_command(case.root, "merge", "--ff-only", "origin/main")
    case.base = case.history.tip


def test_reused_prefix_runs_machine_only_checks_on_the_merged_tree(batched):
    case = batched
    shared_locks(case)
    path = "tests/test_machine_pair.py"
    update_base(case, {path: "import pytest\nfrom pathlib import Path\n"
        "@pytest.mark.local_only\ndef test_machine_pair():\n"
        "    assert not (Path('src/member_5.py').exists() and Path('src/member_1.py').exists())\n"})
    queue_workers(case, [5, 1])
    support.gate(case)
    support.gate(case)
    assert len(case.merges) == 1
    assert not case.tracker.get_pr(support.pr_for(case, 1).number).merged_at
    assert path in case.store.log_path(1).read_text()


def test_prefix_protects_tests_that_started_passing_after_the_previous_member(batched):
    case = batched
    shared_locks(case)
    path = "tests/acceptance/test_gained.py"
    update_base(case, {path: "from pathlib import Path\ndef test_gained():\n"
        "    assert Path('src/member_5.py').exists() and not Path('src/member_1.py').exists()\n"})
    queue_workers(case, [5, 1])
    support.gate(case)
    support.gate(case)
    support.gate(case)
    assert len(case.merges) == 1
    assert not case.tracker.get_pr(support.pr_for(case, 1).number).merged_at
    assert "regression" in events(case)[-1]["reason"]


def test_prefix_scope_is_rechecked_at_the_locked_consumption_boundary(batched, monkeypatch):
    case = batched
    shared_locks(case)
    path = "tests/acceptance/test_interaction.py"
    update_base(case, {path: "from pathlib import Path\ndef test_interaction():\n"
        "    assert not (Path('src/member_5.py').exists() and Path('src/member_1.py').exists())\n"})
    queue_workers(case, [5, 1])
    body = case.store.load_approval(5).body.replace("tests/acceptance/test_own_5.py", path)
    case.tracker.add_issue(80, "Temporary owner", body, labels=["approved"])
    case.tracker.record_label(80, "approved", "human")
    case.store.save_approval(80, body=body, body_sha=body_hash(body),
                             acceptance_blobs={path: case.git.blob_sha(case.base, path)},
                             approved_by="human", approved_at=case.clock[0].isoformat())
    support.gate(case)
    assert len(case.merges) == 1 and len(groups(case)) == 2
    support.gate(case)  # Refresh the second member before its final CI read.
    case.tracker.set_pr_checks(support.pr_for(case, 1).number, "pending")
    lock = StateStore.merge_lock

    @contextmanager
    def required_again(store):
        with lock(store):
            store.clear_approval(80)
            case.tracker.remove_labels(80, ["approved"])
            yield

    monkeypatch.setattr(StateStore, "merge_lock", required_again)
    support.gate(case)
    assert not case.tracker.get_pr(support.pr_for(case, 1).number).merged_at
    assert not refs(case)
    assert any(row["kind"] == "ci-evidence" and row["data"].get("reason") == "scope-changed"
               for row in case.store.flow_records())


def test_retired_generations_stay_spent_after_flow_log_rotation(batched):
    case = batched
    case.pending_batch = True
    queue_workers(case, [5, 1])
    support.gate(case)
    green(case)
    case.tracker.set_pr_checks(groups(case)[0].number, "fail")
    for n in [5, 1]:
        case.tracker.set_pr_checks(support.pr_for(case, n).number, "pending")
    support.gate(case)
    assert not refs(case) and events(case)[-1]["result"] == "unused"
    for i in range(2048):
        case.store.record_flow("observation", index=i)
    assert not events(case)
    for n in [5, 1]:
        case.tracker.set_pr_checks(support.pr_for(case, n).number, "pass")
    support.gate(case)
    assert len(groups(case)) == 2


def test_prefix_creation_identity_survives_temporary_listing_lag(batched, monkeypatch):
    case = batched
    shared_locks(case)
    case.pending_batch = True
    queue_workers(case, [5, 1])
    find, get = case.tracker.find_pr, case.tracker.get_pr
    reads = []

    def direct_read(pr):
        reads.append(pr)
        return get(pr)

    monkeypatch.setattr(case.tracker, "get_pr", direct_read)
    monkeypatch.setattr(case.tracker, "find_pr", lambda branch: None if branch.startswith(batch.PREFIX) else find(branch))
    support.gate(case)
    assert len(groups(case)) == 2 and refs(case) and not events(case)
    assert groups(case)[0].number in reads
    assert not case.merges
    support.gate(case)  # A restarted owner has only the temporarily empty listing.
    assert refs(case) and not events(case) and not case.merges
    monkeypatch.setattr(case.tracker, "find_pr", find)
    green(case)
    support.drain(case, count=2)
    assert len(case.merges) == 2 and len(groups(case)) == 2


@pytest.mark.parametrize("boundary", ["prepare", "consume", "cleanup", "previous"])
def test_batch_github_wait_reaches_owner_pause_without_retirement(batched, monkeypatch, boundary):
    case = batched
    shared_locks(case)
    case.pending_batch = True
    queue_workers(case, [5, 1])
    support.gate(case)
    green(case)
    merged_before = 0
    if boundary == "previous":
        support.gate(case)
        support.gate(case)  # Second member has refreshed, and needs prefix 1 as baseline.
        merged_before = 1
    checks, reports = case.tracker.pr_checks, case.tracker.pr_acceptance_reports
    armed, failed = [boundary != "consume"], []

    def throttled(call, *args):
        if armed[0] and not failed:
            failed.append(True)
            raise TrackerError("HTTP 429 API rate limit exceeded")
        return call(*args)

    if boundary in {"cleanup", "previous"}:
        if boundary == "cleanup":
            case.tracker.set_pr_checks(groups(case)[0].number, "fail")
        monkeypatch.setattr(case.tracker, "pr_acceptance_reports", lambda head: throttled(reports, head))
    else:
        prefix_pr = groups(case)[0].number
        monkeypatch.setattr(case.tracker, "pr_checks", lambda pr: throttled(checks, pr) if pr == prefix_pr else checks(pr))
    if boundary == "consume":
        lock = StateStore.merge_lock

        @contextmanager
        def arm_at_consume(store):
            with lock(store):
                armed[0] = True
                yield

        monkeypatch.setattr(StateStore, "merge_lock", arm_at_consume)
    support.gate(case)
    assert failed and case.store.github_wait_until(case.clock[0]) is not None
    assert refs(case) and not events(case) and len(case.merges) == merged_before
    case.clock[0] += timedelta(minutes=2)
    case.tracker.set_pr_checks(groups(case)[0].number, "pass")
    support.drain(case, count=2)
    assert len(case.merges) == 2 and len(groups(case)) == 2


def test_union_refresh_checks_and_publishes_its_own_tree_before_ci_reuse(batched, monkeypatch):
    case = batched
    shared_locks(case)
    update_base(case, {".gitattributes": "shared.txt merge=union\n", "shared.txt": "anchor\n",
        "tests/test_union_machine.py": "import pytest\nfrom pathlib import Path\n"
        "@pytest.mark.local_only\ndef test_union_machine():\n"
        "    assert Path('shared.txt').read_text() != 'anchor\\na\\nb\\n'\n"})
    model = case.ctx.executor_factory("claude")
    implement = model.run

    def append_member(**kwargs):
        result = implement(**kwargs)
        (kwargs["worktree"] / "shared.txt").write_text("anchor\n" + ("a\n" if case.worker.number == 5 else "b\n"))
        return result

    monkeypatch.setattr(model, "run", append_member)
    queue_workers(case, [5, 1])
    support.gate(case)
    original = case.tracker.pr_head_sha(support.pr_for(case, 1).number)
    support.gate(case)  # Normal refresh verifies and publishes anchor,b,a.
    refreshed = case.tracker.pr_head_sha(support.pr_for(case, 1).number)
    assert refreshed != original
    assert git_command(case.root, "show", f"{refreshed}:shared.txt") == "anchor\nb\na"
    prefix = case.tracker.pr_head_sha(groups(case)[-1].number)
    assert git_command(case.root, "show", f"{prefix}:shared.txt") == "anchor\na\nb"
    case.tracker.set_pr_checks(support.pr_for(case, 1).number, "pending")
    support.gate(case)
    assert len(case.merges) == 1 and not refs(case)  # Different trees must wait for their own CI.
    case.tracker.set_pr_checks(support.pr_for(case, 1).number, "pass")
    support.drain(case, count=2)
    assert len(case.merges) == 2
    assert git_command(case.history.origin, "show", "main:shared.txt") == "anchor\nb\na"


def test_combined_conflict_markers_are_rejected_by_the_normal_refresh_publication_gate(batched, monkeypatch):
    case = batched
    shared_locks(case)
    update_base(case, {"shared.txt": "anchor\nmiddle\nend\n"})
    model = case.ctx.executor_factory("claude")
    implement = model.run

    def markers(**kwargs):
        result = implement(**kwargs)
        content = "anchor\nmiddle\nend\n"
        content = "<<<<<<< HEAD\n" + content if case.worker.number == 5 else content + ">>>>>>> branch\n"
        (kwargs["worktree"] / "shared.txt").write_text(content)
        return result

    monkeypatch.setattr(model, "run", markers)
    queue_workers(case, [5, 1])
    support.drain(case, count=2)
    assert len(case.merges) == 1
    assert not case.tracker.get_pr(support.pr_for(case, 1).number).merged_at
    assert "shared.txt" in "\n".join(c.body for c in case.tracker.list_comments(1))
    assert not refs(case)


@pytest.mark.parametrize("interruption", ["crash", "throttle"])
def test_restart_recreates_missing_prefix_prs_from_refs_without_republishing(batched, monkeypatch, interruption):
    case = batched
    shared_locks(case)
    case.pending_batch = True
    queue_workers(case, [5, 1])
    create = case.tracker.create_pr
    failed = []

    def interrupted(**kwargs):
        if kwargs["head"].startswith(batch.PREFIX) and not failed:
            failed.append(True)
            if interruption == "crash":
                raise KeyboardInterrupt("Owner stopped after push, before PR creation")
            raise TrackerError("HTTP 429 API rate limit exceeded")
        return create(**kwargs)

    monkeypatch.setattr(case.tracker, "create_pr", interrupted)
    if interruption == "crash":
        with pytest.raises(KeyboardInterrupt):
            support.gate(case)
    else:
        support.gate(case)
        assert case.store.github_wait_until(case.clock[0]) is not None
    pushes = list(case.batch_pushes)
    assert refs(case) and not groups(case)
    case.clock[0] += timedelta(minutes=2)
    support.gate(case)
    assert len(groups(case)) == 2 and case.batch_pushes == pushes
    assert not case.merges and not events(case)
    green(case)
    support.drain(case, count=2)
    assert len(case.merges) == 2 and len(groups(case)) == 2 and not refs(case)


def test_existing_four_member_batch_survives_size_reduction(batched):
    case = batched
    shared_locks(case)
    case.pending_batch = True
    queue_workers(case, [5, 1, 4, 2])
    support.gate(case)
    case.config = replace(case.config, merge_batch_size=2)
    case.ctx.config = case.config
    support.gate(case)
    assert len(groups(case)) == 4 and len(refs(case).splitlines()) == 4 and not events(case)
    green(case)
    support.drain(case)
    assert len(case.merges) == 4 and not refs(case)


def test_last_reconciliation_throttle_stops_batch_preparation(batched, monkeypatch):
    case = batched
    queue_workers(case, [5, 1, 4])
    get = case.tracker.get_pr
    last = support.pr_for(case, 4).number
    failed = []

    def throttled(pr):
        if pr == last and not failed:
            failed.append(True)
            raise TrackerError("HTTP 429 API rate limit exceeded")
        return get(pr)

    monkeypatch.setattr(case.tracker, "get_pr", throttled)
    support.gate(case)
    assert failed and case.store.github_wait_until(case.clock[0]) is not None
    assert not case.batch_pushes and not groups(case) and not case.merges
    case.clock[0] += timedelta(minutes=2)
    support.drain(case, count=3)
    assert len(case.merges) == 3


def test_retirement_uses_the_generation_recorded_before_two_refreshes(batched):
    case = batched
    shared_locks(case)
    case.config = replace(case.config, merge_batch=False)
    case.ctx.config = case.config
    queue_workers(case, [5, 1])
    generations = {n: case.store.get_run(n).started_at for n in [5, 1]}
    case.history.update({"src/neighbor.py": "VALUE = 1\n"})
    support.gate(case)
    case.tracker.set_pr_checks(support.pr_for(case, 5).number, "pending")
    support.gate(case)  # Both admissions have now been refreshed once.
    for n in [5, 1]:
        case.tracker.set_pr_checks(support.pr_for(case, n).number, "pass")
    case.config = replace(case.config, merge_batch=True)
    case.ctx.config = case.config
    case.pending_batch = True
    support.gate(case)
    assert len(groups(case)) == 2
    case.history.update({"src/neighbor.py": "VALUE = 2\n"})
    support.gate(case)  # Refresh again before retiring the now obsolete batch.
    case.tracker.set_pr_checks(support.pr_for(case, 5).number, "pending")
    support.gate(case)
    assert not refs(case)
    case.tracker.set_pr_checks(support.pr_for(case, 5).number, "pass")
    assert {m["n"]: m["started_at"] for m in events(case)[-1]["members"]} == generations
    green(case)
    queue_workers(case, [4])
    support.drain(case, count=3)
    assert len(groups(case)) == 2  # Retired generations cannot form a second batch.


def test_pending_prefix_does_not_hold_a_member_with_usable_own_ci(batched):
    case = batched
    shared_locks(case)
    case.pending_batch = True
    support.queue_workers(case, [5, 1])  # Own CI remains green; only prefix CI is pending.
    support.gate(case)
    assert len(case.merges) == 1 and refs(case) and not events(case)
    green(case)
    support.drain(case, count=2)
    assert len(case.merges) == 2 and not refs(case)
