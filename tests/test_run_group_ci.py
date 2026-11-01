from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from foragent import ci_mode, run_baseline, run_group_check, run_group_ci as group_ci, run_group_evidence, run_merge_turn, runner
from foragent.ci_workflow import read_exclusions
from foragent.git import GitError, GitRepo
from foragent.tracker import TrackerError
from conftest import git_command
from test_ci_mode_unit import report
from test_run_group_check import NOW, make_ctx, record_result


@pytest.fixture
def ci_case(tmp_path, monkeypatch):
    ctx = make_ctx(tmp_path, (5, 1, 4))
    ctx.config = replace(ctx.config, checks="ci", ci_shards=2)
    ctx.repo, ctx.repo_root = "owner/repo", Path.cwd()
    clock, calls = [NOW], []
    ctx.now = lambda: clock[0]
    ctx.sleep = lambda seconds: clock.__setitem__(0, clock[0] + timedelta(seconds=seconds))
    reports = [report(), report(1)]
    ctx.git = SimpleNamespace(
        _call=lambda args: calls.append(tuple(args)) or "",
        remote_branch_sha=lambda branch: "head",
    )
    ctx.tracker.create_pr = lambda **kwargs: calls.append(("create", kwargs)) or 99
    ctx.tracker.find_pr = lambda branch: 99
    ctx.tracker.close_pr = lambda pr: calls.append(("close", pr))
    ctx.tracker.pr_acceptance_reports = lambda head: deepcopy(reports)
    monkeypatch.setattr(ci_mode, "_trusted_sha", lambda *args: "trusted")
    monkeypatch.setattr(group_ci, "_baseline_exclusions", lambda *args: [])
    monkeypatch.setattr(runner, "_remaining", lambda *args: 2000)
    return SimpleNamespace(ctx=ctx, calls=calls, reports=reports, clock=clock)


@pytest.mark.parametrize("result", ["passed", "failed", "void", "timeout", "transport", "create"])
def test_leader_publishes_once_judges_exact_reports_and_cleans_up(ci_case, result):
    c = ci_case
    if result == "failed":
        c.reports[0]["report"]["head"]["passing_ids"] = []
    elif result == "void":
        c.reports[0]["report"]["schema_version"] = 99
    elif result == "timeout":
        c.reports.clear()
    elif result in {"transport", "create"}:
        def unavailable(*args, **kwargs):
            raise TrackerError("unavailable")

        if result == "create":
            c.ctx.tracker.create_pr = unavailable
        else:
            c.ctx.tracker.pr_acceptance_reports = unavailable
    verdict, reason, metadata = group_ci.check(5, c.ctx, "main", "base", "head", excluded=[])
    assert verdict == (result if result in {"passed", "failed"} else "void")
    assert (reason is None) == (result == "passed")
    assert metadata["combined_sha"] == "head"
    branch = "foragent/group-5-head"
    pushes = [call for call in c.calls if call[0] == "push"]
    assert pushes == [
        ("push", f"--force-with-lease=refs/heads/{branch}:", "origin", f"head:refs/heads/{branch}"),
        ("push", "origin", "--delete", branch),
    ]
    created = [call[1] for call in c.calls if call[0] == "create"]
    if result != "create":
        assert len(created) == 1
        assert created[0]["head"] == branch and created[0]["base"] == "main"
        assert "Closes #" not in created[0]["body"]
        assert ("close", 99) in c.calls
    assert (c.clock[0] - NOW).total_seconds() == (1200 if result == "timeout" else 0)


@pytest.mark.parametrize("damage", ["head", "base", "scope", "shard", "workflow"])
def test_group_ci_rejects_uncovered_measurements(ci_case, damage, monkeypatch):
    c = ci_case
    if damage in {"head", "base"}:
        c.reports[0]["report"][f"{damage}_sha"] = "wrong"
    elif damage == "scope":
        c.reports[0]["report"]["scope"]["marker"] = "everything"
    elif damage == "shard":
        c.reports.pop()
    else:
        monkeypatch.setattr(ci_mode, "changed_workflows", lambda *args: "changed.yml")
    verdict, reason, _ = group_ci.check(5, c.ctx, "main", "base", "head", excluded=[])
    assert verdict == "void" and reason


@pytest.mark.parametrize("result", ["passed", "failed", "outage", "rate-limit", "timeout"])
def test_ci_groups_use_auto_limit_and_journal_results_for_next_group(ci_case, monkeypatch, result):
    c = ci_case
    c.ctx.config = replace(c.ctx.config, merge_group_max="auto")
    record_result(c.ctx.store, "failed", 4)
    record_result(c.ctx.store, "failed", 3)
    c.ctx.tracker.find_pr = lambda branch: int(branch.rsplit("-", 1)[1]) if "/issue-" in branch else 99
    monkeypatch.setattr(ci_mode, "enabled", lambda *args: True)
    monkeypatch.setattr(run_baseline, "_baseline_exclusions", lambda *args: [])
    combined_members = []
    monkeypatch.setattr(run_group_check, "_combine", lambda git, base, members, **kwargs: (
        combined_members.append([m.n for m in members]) or "head"))
    monkeypatch.setattr(run_group_check, "_trees", lambda *args: ["t5", "t1"])
    if result == "failed":
        c.reports[0]["report"]["head"]["passing_ids"] = []
    elif result == "timeout":
        c.reports.clear()
    elif result in {"outage", "rate-limit"}:
        def unavailable(*args):
            raise TrackerError("HTTP 429" if result == "rate-limit" else "unavailable")

        c.ctx.tracker.pr_acceptance_reports = unavailable
    run_group_check.check(5, c.ctx, run_merge_turn.seat(c.ctx, 5, "main"), "h5", "base", set())
    assert combined_members == [[5, 1]]
    event, _ = run_group_evidence.latest(c.ctx, 1)
    assert event["mode"] == "ci"
    assert [m["n"] for m in event["members"]] == [5, 1]
    assert event["verdict"] == (result if result in {"passed", "failed"} else "void")
    assert run_group_check.group_limit(c.ctx.store, c.ctx.config) == (3 if result == "passed" else 2, "auto")


@pytest.mark.parametrize("damage", [None, "provenance", "scope", "reports", "workflow"])
def test_member_and_locked_read_require_provenance_and_unchanged_ci(ci_case, monkeypatch, damage):
    c = ci_case
    _, _, metadata = group_ci.check(5, c.ctx, "main", "base", "head", excluded=[])
    event = {**metadata, "mode": "ci", "base_sha": "base", "verdict": "passed"}
    found = (event, 0)
    monkeypatch.setattr(run_group_evidence, "latest", lambda *args: found)
    reads = []
    monkeypatch.setattr(run_group_evidence, "void_reason", lambda ctx, n, head, base, *args: (
        reads.append((n, head, base)) or ("outsider" if damage == "provenance" else None)))
    view = SimpleNamespace(n=5, ctx=c.ctx, git=c.ctx.git)
    evidence = group_ci.evidence(view, "member-head", "base")
    if damage == "provenance":
        assert evidence is None
        return
    assert evidence is not None
    if damage == "scope":
        c.ctx.config = replace(c.ctx.config, ci_shards=3)
    elif damage == "reports":
        c.reports[0]["report"]["head"]["passing_ids"] = []
    elif damage == "workflow":
        monkeypatch.setattr(ci_mode, "changed_workflows", lambda *args: "changed.yml")
    assert evidence.current(5, c.ctx, c.ctx.git, 99, "member-head", "later-main", "pass") == (damage is None)
    assert reads == [(5, "member-head", "base"), (5, "member-head", "later-main")]


@pytest.mark.parametrize("verdict", ["passed", "failed", "void"])
def test_ci_group_dispatch_records_exact_members_once_and_followers_fall_back(ci_case, monkeypatch, verdict):
    c = ci_case
    calls = []
    c.ctx.tracker.find_pr = lambda branch: int(branch.rsplit("-", 1)[1])
    monkeypatch.setattr(ci_mode, "enabled", lambda *args: True)
    monkeypatch.setattr(run_baseline, "_baseline_exclusions", lambda *args: ["pending.py"])
    monkeypatch.setattr(run_group_check, "_combine", lambda git, base, members, **kwargs: (
        calls.append((base, [(m.n, m.head_sha) for m in members], kwargs)) or "combined"))
    monkeypatch.setattr(run_group_check, "_trees", lambda *args: ["tree-5", "tree-1", "tree-4"])
    monkeypatch.setattr(group_ci, "check", lambda *args, **kwargs: (verdict, None, {"combined_sha": "combined"}))
    done = set()
    place = run_merge_turn.seat(c.ctx, 5, "main")
    run_group_check.check(5, c.ctx, place, "h5", "base", done)
    run_group_check.check(5, c.ctx, place, "h5", "base", done)
    # Even after fallback refreshes a follower's head, this run does not form
    # another group. Its existing individual verification path takes over.
    run_group_check.check(1, c.ctx, run_merge_turn.seat(c.ctx, 1, "main"), "refreshed", "later-main", set())
    assert calls == [("base", [(5, "h5"), (1, "h1"), (4, "h4")], {"excluded": ["pending.py"]})]
    event, index = run_group_evidence.latest(c.ctx, 1)
    assert index == 1 and event["issue"] == 5 and event["mode"] == "ci"
    assert event["base_sha"] == "base" and event["verdict"] == verdict
    assert event["members"] == [
        {"n": 5, "head_sha": "h5", "tree": "tree-5"},
        {"n": 1, "head_sha": "h1", "tree": "tree-1"},
        {"n": 4, "head_sha": "h4", "tree": "tree-4"},
    ]


def test_group_uses_one_exclusion_snapshot_for_the_commit_and_ci_comparison(ci_case, monkeypatch):
    c = ci_case
    live = ["pending.py"]
    reads, combined = [], []
    for value in c.reports:
        value["report"]["excluded"] = list(live)
        value["report"]["scope"]["excluded"] = list(live)
    c.ctx.tracker.find_pr = lambda branch: int(branch.rsplit("-", 1)[1]) if "/issue-" in branch else 99

    def exclusions(ctx, base, log):
        reads.append(base)
        return list(live)

    def combine(git, base, members, *, excluded):
        combined.append((base, list(excluded)))
        # The pending owners can change while the combined commit is built.
        # Its CI comparison must use the selection already put in its trailer.
        live.clear()
        return "head"

    monkeypatch.setattr(ci_mode, "enabled", lambda *args: True)
    monkeypatch.setattr(run_baseline, "_baseline_exclusions", exclusions)
    monkeypatch.setattr(group_ci, "_baseline_exclusions", exclusions)
    monkeypatch.setattr(run_group_check, "_combine", combine)
    monkeypatch.setattr(run_group_check, "_trees", lambda *args: ["t5", "t1", "t4"])
    run_group_check.check(5, c.ctx, run_merge_turn.seat(c.ctx, 5, "main"), "h5", "base", set())
    event, _ = run_group_evidence.latest(c.ctx, 5)
    assert reads == ["base"] and combined == [("base", ["pending.py"])]
    assert event["verdict"] == "passed" and event["mode"] == "ci"
    assert event["scope"]["excluded"] == ["pending.py"]
    assert c.calls.count(("close", 99)) == 1
    # Spending a pass still checks today's required scope, including under the
    # merge lock: freezing this later read would weaken individual CI's bar.
    monkeypatch.setattr(run_group_evidence, "void_reason", lambda *args: None)
    assert group_ci.evidence(SimpleNamespace(n=5, ctx=c.ctx, git=c.ctx.git), "h5", "base") is None
    assert reads == ["base", "base"]


@pytest.mark.parametrize("stage", ["exclusions", "combine", "trees"])
def test_ci_preparation_fault_records_mode_and_never_regroups_its_members(ci_case, monkeypatch, stage):
    c = ci_case
    prepared = []
    c.ctx.tracker.find_pr = lambda branch: int(branch.rsplit("-", 1)[1])
    monkeypatch.setattr(ci_mode, "enabled", lambda *args: True)
    monkeypatch.setattr(run_baseline, "_baseline_exclusions", lambda *args: [])
    monkeypatch.setattr(run_group_check, "_combine", lambda *args, **kwargs: "head")
    monkeypatch.setattr(run_group_check, "_trees", lambda *args: ["t5", "t1", "t4"])

    def fault(*args, **kwargs):
        prepared.append(stage)
        raise GitError(f"unreadable {stage}")

    if stage == "exclusions":
        monkeypatch.setattr(run_baseline, "_baseline_exclusions", fault)
    else:
        monkeypatch.setattr(run_group_check, "_" + stage, fault)
    run_group_check.check(5, c.ctx, run_merge_turn.seat(c.ctx, 5, "main"), "h5", "base", set())
    event, index = run_group_evidence.latest(c.ctx, 1)
    assert index == 1 and event["issue"] == 5 and event["mode"] == "ci"
    assert event["verdict"] == "void" and event["passed"] is False
    assert event["reason"] == f"unreadable {stage}"
    run_group_check.check(1, c.ctx, run_merge_turn.seat(c.ctx, 1, "main"), "h1", "base", set())
    assert prepared == [stage]
    assert run_group_evidence.latest(c.ctx, 1)[0] == event
    assert not any(call[0] in {"push", "create", "close"} for call in c.calls)


@pytest.mark.parametrize("excluded", [None, [], ["pending-b.py", "pending-a.py", "pending-b.py"]])
@pytest.mark.parametrize("cut", [False, True])
def test_combined_ci_commit_carries_the_exclusions_protocol_without_changing_trees(git_history, excluded, cut):
    history = git_history
    base = history.tip
    history.update({"member-5.py": "VALUE = 5\n"})
    head5 = history.tip
    history.update({"member-1.py": "VALUE = 1\n"})
    head1 = history.tip
    members = [run_group_check.Member(5, head5, ()), run_group_check.Member(1, head1, ())]
    if cut:
        history.tip = base
        git_command(history.origin, "update-ref", "refs/heads/main", base)
        history.update({"member-5.py": "CONFLICT = True\n"})
        members.extend([run_group_check.Member(4, history.tip, ()),
                        run_group_check.Member(7, "must-not-be-read", ())])
    git = GitRepo(history.origin)
    combined = run_group_check._combine(git, base, members, excluded=excluded)
    trees = run_group_check._trees(git, base, combined)
    assert len(trees) == 2
    assert trees[-1] == git_command(history.origin, "rev-parse", f"{head1}^{{tree}}")
    git_command(history.origin, "update-ref", "refs/heads/main", combined)
    # This is the actual reader ci_shard uses on its checkout, not a fake CI
    # transport: None would skip measurement and publish no reports at all.
    assert read_exclusions(git, base) == (None if excluded is None else sorted(set(excluded)))


@pytest.mark.parametrize("damage", [
    "missing-run-id", "non-object-run", "non-json-extra", "circular-extra", "overflow-duration",
    "transport-key", "transport-type", "transport-json", "transport-recursion", "transport-overflow",
])
def test_malformed_group_reports_record_void_and_clean_up_for_individual_fallback(ci_case, monkeypatch, damage):
    c = ci_case
    c.ctx.tracker.find_pr = lambda branch: int(branch.rsplit("-", 1)[1]) if "/issue-" in branch else 99
    monkeypatch.setattr(ci_mode, "enabled", lambda *args: True)
    monkeypatch.setattr(run_baseline, "_baseline_exclusions", lambda *args: [])
    monkeypatch.setattr(run_group_check, "_combine", lambda *args, **kwargs: "head")
    monkeypatch.setattr(run_group_check, "_trees", lambda *args: ["t5", "t1", "t4"])
    if damage in {"missing-run-id", "non-object-run"}:
        digest = group_ci._report_digest

        def changed_after_validation(values):
            result = digest(values)
            # Exercise the summary read after the real comparison succeeded:
            # validation must not leave later parsing outside the recovery boundary.
            if damage == "missing-run-id":
                values[0]["run"].pop("id")
            else:
                values[0]["run"] = None
            return result

        monkeypatch.setattr(group_ci, "_report_digest", changed_after_validation)
    elif damage == "non-json-extra":
        c.reports[0]["extra"] = {"not-json"}
    elif damage == "circular-extra":
        c.reports[0]["extra"] = c.reports
    elif damage == "overflow-duration":
        c.reports[0]["report"]["duration_seconds"] = 10 ** 1000
    else:
        error = {"transport-key": KeyError, "transport-type": TypeError, "transport-json": ValueError,
                 "transport-recursion": RecursionError, "transport-overflow": OverflowError}[damage]

        def unreadable(head):
            raise error("invalid acceptance reports")

        c.ctx.tracker.pr_acceptance_reports = unreadable
    run_group_check.check(5, c.ctx, run_merge_turn.seat(c.ctx, 5, "main"), "h5", "base", set())
    event, index = run_group_evidence.latest(c.ctx, 1)
    assert index == 1 and event["issue"] == 5
    assert event["verdict"] == "void" and event["passed"] is False and event["reason"]
    assert c.calls.count(("close", 99)) == 1
    assert [call for call in c.calls if call[0] == "push"] == [
        ("push", "--force-with-lease=refs/heads/foragent/group-5-head:", "origin", "head:refs/heads/foragent/group-5-head"),
        ("push", "origin", "--delete", "foragent/group-5-head"),
    ]
    view = SimpleNamespace(n=1, ctx=c.ctx, git=c.ctx.git)
    assert group_ci.evidence(view, "h1", "base") is None


@pytest.mark.parametrize("damage", [
    "non-json-extra", "circular-extra", "transport-key", "transport-type", "transport-json", "transport-recursion", "transport-overflow",
    "provenance-key", "provenance-type", "provenance-value",
])
def test_malformed_group_reports_at_locked_read_reject_reuse(ci_case, monkeypatch, damage):
    c = ci_case
    verdict, _, metadata = group_ci.check(5, c.ctx, "main", "base", "head", excluded=[])
    assert verdict == "passed"
    found = ({**metadata, "mode": "ci", "base_sha": "base", "verdict": "passed"}, 0)
    monkeypatch.setattr(run_group_evidence, "latest", lambda *args: found)
    monkeypatch.setattr(run_group_evidence, "void_reason", lambda *args: None)
    view = SimpleNamespace(n=5, ctx=c.ctx, git=c.ctx.git)
    evidence = group_ci.evidence(view, "h5", "base")
    assert evidence is not None
    writes = list(c.calls)
    if damage == "non-json-extra":
        c.reports[0]["extra"] = {"not-json"}
    elif damage == "circular-extra":
        c.reports[0]["extra"] = c.reports
    else:
        error = {"key": KeyError, "type": TypeError, "json": ValueError, "value": ValueError,
                 "recursion": RecursionError, "overflow": OverflowError}[damage.split("-")[1]]

        def unreadable(*args):
            raise error("invalid evidence")

        if damage.startswith("provenance-"):
            monkeypatch.setattr(run_group_evidence, "void_reason", unreadable)
        else:
            c.ctx.tracker.pr_acceptance_reports = unreadable
    assert evidence.current(5, c.ctx, c.ctx.git, 99, "h5", "base", "pass") is False
    assert [call for call in c.calls if call[0] in {"push", "create", "close"}] == [
        call for call in writes if call[0] in {"push", "create", "close"}]
