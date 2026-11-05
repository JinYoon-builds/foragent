"""Derive ledger facts from Git and the existing GitHub issue cache.

Real CLI, Git attributes/history and state readers; existing offline publication
adapters import Git objects and transfer local refs without commit/push commands.
Legacy wire formats below are migration inputs, not required replacement APIs.
Unpublished intent and Foragent's execution/approval evidence remain local facts.
"""

from dataclasses import asdict, replace
from datetime import timedelta
import json
from types import SimpleNamespace

import pytest

from conftest import git_command
from foragent import capacity, runner
from foragent.config import Config
from foragent.git import GitRepo
from foragent.runner import RunContext
from foragent.scheduler import tick
from foragent.state import StateStore
from foragent.tracker import FakeTracker
from test_d85_batched_publication import pending
from test_d85_flush_recovery import (
    Interrupted, finish, offline_publication, plan, publishing, queue_batch,
    recovery, stop,
)
from test_lane_clone import _lane_clone_issue
from test_lane_width_by_overlap import _wide_lane
from test_local_truth import record_gh
from test_orch_facts import NOW, approved_issue, completed, facts_case
from test_orch_review_handoff import make_case
from test_order_preread import preread_case  # noqa: F401
from test_scheduler import ctx  # noqa: F401
from test_skills_autosync import cli_exit, isolated_home  # noqa: F401


SHARED = "src/foragent/shared.py"
UNION_LEDGER = "union-merge-paths.json"


def json_rows(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def read_facts(case, capsys):
    capsys.readouterr()
    code = cli_exit(case.main, ["orch", "facts", "--repo-root", str(case.root), "--json"])
    captured = capsys.readouterr()
    assert code == 0, captured.out + captured.err
    assert case.gh_calls == [], "Local facts must use Git and the existing issue cache"
    return json.loads(captured.out)


def attributes(history, union):
    history.update({".gitattributes": f"{SHARED} merge={'union' if union else 'binary'}\n"}, at=NOW)
    GitRepo(history.clone).fetch("origin", "main")


def overlap_case(git_history, tmp_path, monkeypatch):
    case = facts_case(tmp_path, monkeypatch)
    case.root = git_history.clone
    (case.root / ".foragent.yml").write_text(
        "repo: owner/repo\nlanguage: en\nskill_libraries: []\nnotify: none\n"
        "max_parallel: 4\noverlap_per_file: 1\nlanes: {main: {paths: ['src/**']}}\n"
    )
    capacity.write_state(case.store, 4, NOW)
    approved_issue(case, 1, [SHARED], labels=("approved", "running"), active=True)
    approved_issue(case, 2, [SHARED])
    return case


def assert_overlap(case, capsys, *, union):
    work = read_facts(case, capsys)["resources"]["parallelizable_work"]
    assert work["waiting_issues"] == [2]
    assert work["free_run_slots"] == 3
    assert work["held"] == ({} if union else {"2": "structure"}), (
        "The current Git attribute must determine the overlap exemption, without a gate/state update"
    )


def test_facts_read_union_attributes_without_a_previously_written_ledger(
    git_history, tmp_path, monkeypatch, capsys,
):
    case = overlap_case(git_history, tmp_path, monkeypatch)
    attributes(git_history, True)
    assert not (case.store.root / UNION_LEDGER).exists()
    assert_overlap(case, capsys, union=True)
    assert not (case.store.root / UNION_LEDGER).exists(), "Reading attributes must not copy them into state"


@pytest.mark.parametrize("was_union", [False, True], ids=["enable-union", "remove-union"])
def test_facts_follow_changed_attributes_instead_of_a_legacy_ledger(
    git_history, tmp_path, monkeypatch, capsys, was_union,
):
    case = overlap_case(git_history, tmp_path, monkeypatch)
    attributes(git_history, was_union)
    legacy = case.store.root / UNION_LEDGER
    legacy.write_text(json.dumps({"paths": {SHARED: was_union}}))
    assert_overlap(case, capsys, union=was_union)
    before = legacy.read_bytes() if legacy.exists() else None
    attributes(git_history, not was_union)
    assert (legacy.read_bytes() if legacy.exists() else None) == before
    assert_overlap(case, capsys, union=not was_union)


@pytest.mark.parametrize("union", [False, True], ids=["ordinary-path", "union-path"])
def test_guard_facts_honor_the_current_limit_with_a_matching_legacy_ledger(
    git_history, tmp_path, monkeypatch, capsys, union,
):
    case = overlap_case(git_history, tmp_path, monkeypatch)
    attributes(git_history, union)
    (case.store.root / UNION_LEDGER).write_text(json.dumps({"paths": {SHARED: union}}))
    assert_overlap(case, capsys, union=union)


@pytest.mark.parametrize("union", [False, True], ids=["ordinary-path-limit", "union-path-exemption"])
def test_guard_scheduler_admits_the_exact_number_allowed_by_git_attributes(ctx, git_history, union):
    starts = _wide_lane(ctx, 4)
    ctx.config = replace(ctx.config, overlap_per_file=1)
    attributes(git_history, union)
    for number in (1, 2, 3):
        _lane_clone_issue(ctx, number, [SHARED])
    report = tick(ctx)
    assert report.skipped is None, report
    expected = [1, 2, 3] if union else [1]
    assert starts == expected, report.actions
    assert sorted(run.number for run in ctx.store.active_runs()) == expected
    assert sorted(number for number in (1, 2, 3)
                  if "ready" in ctx.tracker.get_issue(number).labels) == ([] if union else [2, 3])


def test_scheduler_does_not_write_a_second_copy_of_git_attributes(ctx, git_history):
    starts = _wide_lane(ctx, 4)
    attributes(git_history, True)
    for number in (1, 2, 3):
        _lane_clone_issue(ctx, number, [SHARED])
    report = tick(ctx)
    assert report.skipped is None, report
    assert starts == [1, 2, 3]
    assert not (ctx.store.root / UNION_LEDGER).exists(), (
        "Admission still writes union-merge-paths.json, a second copy of Git attributes"
    )


class IncrementalGithub(FakeTracker):
    """The public incremental listing offered by GhTracker, entirely offline."""

    def merged_prs_updated_since(self, since, head_prefix=""):
        return [{**asdict(pr), "updated_at": pr.merged_at}
                for pr in self.list_prs("merged", head_prefix)
                if since is None or pr.merged_at >= since.isoformat()]


@pytest.fixture
def github_case(git_history, tmp_path, monkeypatch):
    tracker = IncrementalGithub(now=lambda: NOW)
    tracker.add_issue(7, "Implement feature", "Feature contract", labels=["approved"])
    store = StateStore(tmp_path / "state", now=lambda: NOW)
    context = RunContext("owner/repo", git_history.clone, tracker, store, Config(),
                         GitRepo(git_history.clone), now=lambda: NOW)
    return SimpleNamespace(tracker=tracker, store=store, context=context, gh_calls=record_gh(monkeypatch))


def merged_row(case):
    case.context.tracker.refresh_issues(observed_at=NOW)
    rows = [row for row in StateStore(case.store.root).local_issues() if row["number"] == 7]
    assert len(rows) == 1
    assert case.gh_calls == []
    return rows[0]


def merge_feature(case):
    pr = case.tracker.create_pr(head="foragent/issue-7", base="main", title="Feature", body="Closes #7")
    case.tracker.merge_pr(pr, expected_head_sha=case.tracker.pr_head_sha(pr))
    assert case.tracker.merged_prs == [pr]
    return pr


def test_guard_refresh_keeps_each_merge_once_in_the_existing_issue_cache(github_case):
    case = github_case
    pr = merge_feature(case)
    for _ in range(2):
        row = merged_row(case)
        assert (row["pr_number"], row["merged"], row["merged_at"]) == (pr, True, NOW.isoformat())
        assert list(row["merges"]) == [str(pr)], "Repeated refresh must not duplicate a merge"
        assert row["merges"][str(pr)]["merged_at"] == NOW.isoformat()
    assert case.tracker.merged_prs == [pr]


def test_refresh_does_not_write_a_duplicate_merged_pr_listing(github_case):
    case = github_case
    pr = merge_feature(case)
    assert merged_row(case)["pr_number"] == pr
    assert not (case.store.root / "merged_prs.json").exists(), (
        "Merged PR facts already live in issues.json; refresh must delete the duplicate writer"
    )


def test_refresh_ignores_a_legacy_merged_pr_claim_that_github_does_not_make(github_case):
    case = github_case
    case.store.root.mkdir(parents=True, exist_ok=True)
    (case.store.root / "merged_prs.json").write_text(json.dumps({
        "watermark": NOW.isoformat(), "prs": [{
            "number": 70, "head": "foragent/issue-7", "base": "main",
            "merged_at": NOW.isoformat(), "updated_at": NOW.isoformat(),
            "url": "https://github.com/owner/repo/pull/70",
        }],
    }))
    row = merged_row(case)
    assert case.tracker.merged_prs == []
    assert (row["pr_number"], row["merged"], row.get("merged_at"), row.get("merges", {})) == (
        None, False, None, {},
    ), "A legacy merged_prs.json must not manufacture a merge absent from GitHub/the issue cache"


def test_finished_runs_do_not_copy_the_github_merge_timestamp(git_history, tmp_path, monkeypatch):
    case = make_case(git_history, tmp_path, monkeypatch, skill_libraries=[], test_command="true")
    record_gh(monkeypatch)
    assert runner.run_issue(1, case.ctx) == "merged"
    assert len(case.tracker.created_prs) == 1
    assert case.tracker.merged_prs == [case.tracker.created_prs[0].number]
    case.ctx.tracker.refresh_issues(observed_at=case.clock[0])
    reopened = StateStore(case.store.root)
    history = reopened.read_history()
    assert len(history) == 1 and history[0].number == 1 and history[0].outcome == "merged"
    assert reopened.load_approval(1) is not None, "Approval evidence is still a Foragent fact"
    row, = [row for row in reopened.local_issues() if row["number"] == 1]
    assert row["merged"] is True
    assert row["merged_at"] == case.tracker.created_prs[0].merged_at
    records = [("runs/1.json", json.loads((case.store.root / "runs/1.json").read_text())),
               *(("runs.jsonl", row) for row in json_rows(case.store.root / "runs.jsonl"))]
    duplicates = [(name, key) for name, row in records
                  for key in ("merged_at", "owner_merge", "owner_merge_started_at") if key in row]
    assert duplicates == [], f"Run records must derive GitHub merge facts instead of copying them: {duplicates}"


def test_facts_ignore_legacy_run_merge_claims_when_the_issue_cache_has_no_merge(
    tmp_path, monkeypatch, capsys,
):
    case = facts_case(tmp_path, monkeypatch)
    approved_issue(case, 7, [SHARED])
    completed(case, 7, started=NOW - timedelta(hours=1), outcome="error", detail="Worker failed")
    for filename in ("runs/7.json", "runs.jsonl"):
        path = case.store.root / filename
        rows = json_rows(path) if filename.endswith("jsonl") else [json.loads(path.read_text())]
        for row in rows:
            row.update(merged_at=NOW.isoformat(), owner_merge_started_at=row["started_at"], owner_merge={
                "pr": 70, "target": "main", "head": "a" * 40, "merged_at": NOW.isoformat(),
            })
        path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
    result = read_facts(case, capsys)
    assert len(result["runs"]) == 1 and result["runs"][0]["issue"] == 7
    assert result["merges_since_tidy"] == 0, "Legacy run snapshots/history cannot invent a GitHub merge"


def test_facts_count_a_cached_github_merge_without_a_run_history_copy(tmp_path, monkeypatch, capsys):
    case = facts_case(tmp_path, monkeypatch)
    started = NOW - timedelta(hours=1)
    approved_issue(case, 7, [SHARED])
    completed(case, 7, started=started, outcome="error", detail="Worker lost the publication response")
    github = FakeTracker(now=lambda: NOW)
    github.add_issue(7, "Feature", "Feature contract", labels=["approved"], state="closed")
    pr = github.create_pr(head="foragent/issue-7", base="main", title="Feature", body="Closes #7")
    github.merge_pr(pr, expected_head_sha=github.pr_head_sha(pr))
    case.store.refresh_local_issues(github.list_issues(), github.list_prs("merged", "foragent/"), {}, NOW)
    cached, = [row for row in StateStore(case.store.root).local_issues() if row["number"] == 7]
    assert list(cached["merges"]) == [str(pr)]
    assert cached["merges"][str(pr)]["started_at"] == started.isoformat()
    for _ in range(2):
        result = read_facts(case, capsys)
        assert len(result["runs"]) == 1 and result["runs"][0]["issue"] == 7
        assert result["merges_since_tidy"] == 1, (
            "The existing issue cache proves this execution merged, even when the worker lost its response"
        )


def test_published_order_journal_does_not_retain_the_published_plan(recovery, capsys):
    values = queue_batch(recovery, capsys)
    finish(recovery, capsys, values)
    store = StateStore.for_repo("owner/repo")
    retained = [row["id"] for row in json_rows(store.root / "orders.jsonl")
                if any(key in row for key in ("plan", "files"))
                and row["id"] in {value["id"] for value in values}]
    assert retained == [], "Published plans now live in Git and GitHub, not orders.jsonl"


def test_pending_does_not_trust_a_legacy_copy_of_a_published_plan(recovery, capsys):
    case = recovery
    values = queue_batch(case, capsys)
    store = StateStore.for_repo("owner/repo")
    journal = store.root / "orders.jsonl"
    legacy = json_rows(journal)
    finish(case, capsys, values)
    stale = "This title exists only in the obsolete order ledger"
    for row in legacy:
        row["plan"]["title"] = stale
    journal.write_text("\n".join(json.dumps(row) for row in legacy) + "\n")
    shown = pending(case, capsys)
    assert stale not in json.dumps(shown), "Published orders must ignore old plan copies or read Git/GitHub"


def test_flush_journal_does_not_copy_the_github_issue_map(recovery, capsys):
    values = queue_batch(recovery, capsys)
    finish(recovery, capsys, values)
    store = StateStore.for_repo("owner/repo")
    copies = [row for row in json_rows(store.root / "flush-journal.jsonl") if "mapping" in row]
    assert copies == [], "The GitHub parent/child markers already retain the published issue map"


def test_guard_publication_does_not_write_legacy_git_checkpoints(recovery, capsys):
    values = queue_batch(recovery, capsys)
    finish(recovery, capsys, values)
    store = StateStore.for_repo("owner/repo")
    checkpoints = [row for row in json_rows(store.root / "flush-journal.jsonl")
                   if row["kind"] in {"committed", "pushed", "rebuilt"}]
    assert checkpoints == [], "Git history/ref facts must not be copied into flush checkpoints"


@pytest.mark.parametrize("edge", ["commit-response", "push-response", "issue-response"])
def test_guard_recovery_uses_git_and_github_despite_bogus_legacy_checkpoints(
    recovery, capsys, monkeypatch, edge,
):
    case = recovery
    values = queue_batch(case, capsys)
    initial = case.git_adapter.head_sha()
    links = []
    add_sub_issue = case.tracker.add_sub_issue

    def link(parent, child):
        links.append((parent, child))
        return add_sub_issue(parent, child)

    monkeypatch.setattr(case.tracker, "add_sub_issue", link)
    fired = []
    with monkeypatch.context() as patch:
        if edge == "commit-response":
            original = case.git_adapter.commit_paths

            def interrupt(*args, **kwargs):
                original(*args, **kwargs)
                fired.append(edge)
                raise Interrupted()

            patch.setattr(case.git_adapter, "commit_paths", interrupt)
        elif edge == "push-response":
            original = case.git_adapter.push_base

            def interrupt(*args, **kwargs):
                original(*args, **kwargs)
                fired.append(edge)
                raise Interrupted()

            patch.setattr(case.git_adapter, "push_base", interrupt)
        else:
            original = case.tracker.create_issue

            def interrupt(*args, **kwargs):
                number = original(*args, **kwargs)
                if len(case.tracker.list_issues()) == 2 and not fired:
                    fired.append(edge)
                    raise Interrupted()
                return number

            patch.setattr(case.tracker, "create_issue", interrupt)
        stop(case, capsys)
    assert fired == [edge], "The specified effect must happen before its response is lost"
    store = StateStore.for_repo("owner/repo")
    journal = store.root / "flush-journal.jsonl"
    batch = next((row["batch"] for row in json_rows(journal) if row["kind"] == "selected"), "legacy-unused")
    with journal.open("a") as stream:
        for kind in ("committed", "pushed", "rebuilt"):
            stream.write(json.dumps({"kind": kind, "batch": batch, "sha": initial,
                                     "old": "0" * 40}) + "\n")
    finish(case, capsys, values)
    commits = [event for event in case.events if event[0] == "commit"]
    assert len(commits) == 1
    assert len([event for event in case.events if event[0] == "push"]) == 1
    assert len([event for event in case.events if event[0] == "issue"]) == 6
    expected_links = []
    for value in values:
        parent, = [issue for issue in case.tracker.list_issues() if issue.title == value["title"]]
        children = {item["key"]: next(issue.number for issue in case.tracker.list_issues()
                                     if issue.title == item["title"]) for item in value["issues"]}
        assert sorted(issue.number for issue in case.tracker.sub_issues(parent.number)) == sorted(children.values())
        expected_links.extend((parent.number, number) for number in children.values())
        assert [dep.number for dep in case.tracker.blocked_by(children["client"])] == [children["api"]]
    assert sorted(links) == sorted(expected_links), "Each native parent/child link is published exactly once"
    case.git_adapter.fetch("origin", "main")
    assert git_command(case.root, "log", "--format=%s", "origin/main").splitlines().count(commits[0][1]) == 1
    for name, content in commits[0][2].items():
        expected = git_command(case.root, "hash-object", "--stdin", input=content)
        assert git_command(case.root, "rev-parse", f"origin/main:{name}") == expected
