"""Owner reuse requires CI evidence and forward, disjoint memory additions/edits."""

from dataclasses import asdict, replace
import json

import pytest

from acceptance.test_merge_path_real_settings import world  # noqa: F401
from conftest import git_command
from test_merge_owner_proto import proto  # noqa: F401
import test_merge_owner_proto as support
from foragent import ci_local, merge_owner
from foragent.git import GitRepo
from foragent.issue import body_hash
from foragent.run_base_move import REUSED
from foragent.tracker import RejectedEvidence


def waiting(case, monkeypatch):
    if case.config.checks == "local":
        monkeypatch.setattr(case.ci, "reports", lambda row: [])
    support.queue_workers(case, [5])
    row = next(iter(merge_owner.admissions(case.store).values()))["publication"]
    assert case.tracker.pr_checks(row["pr"]) == "pass"
    pushes = []
    push = GitRepo.push_head

    def published(git, *args, **kwargs):
        pushes.append(git.head_sha())
        return push(git, *args, **kwargs)

    monkeypatch.setattr(GitRepo, "push_head", published)
    assert "baseline" not in row and "comparison_baselines" not in row
    return row, pushes, len(case.ci.rounds)


def assert_reused(case, admitted, pushes, rounds):
    accepted_base = case.history.tip
    support.gate(case)
    assert pushes == [], "A neutral move pushed the verified PR again"
    assert len(case.ci.rounds) == rounds, "A neutral move requested another CI round"
    assert [merge["head"] for merge in case.merges] == [admitted["head"]]
    assert not merge_owner.pending(case.store, 5)
    job = json.loads(merge_owner._path(case.ctx, 5).read_text())
    assert job["base"] == admitted["base"]
    assert job["comparison"]["accepted_sha"] == accepted_base
    assert job["merge_base"] == accepted_base
    assert job["ready_at"] == admitted["ready_at"]
    assert job["comparison"]["accepted_sha"] != admitted["base"]
    for field in ("base_sha", "head_sha", "reports", "scope"):
        assert job["comparison"][field] == admitted["comparison"][field]
    assert REUSED in case.store.log_path(5).read_text()


def assert_refreshed(case, admitted, pushes, rounds):
    support.gate(case)
    assert not case.merges, "A non-neutral move merged before refreshed-head CI"
    assert len(pushes) == 1 and pushes[0] != admitted["head"]
    assert len(case.ci.rounds) == rounds + 1
    assert case.ci.rounds[-1].head == pushes[0]
    assert case.ci.rounds[-1].base == case.history.tip
    assert merge_owner.pending(case.store, 5)
    support.gate(case)
    assert [merge["head"] for merge in case.merges] == pushes
    assert len(case.ci.rounds) == rounds + 1
    assert not merge_owner.pending(case.store, 5)


def test_owner_keeps_green_head_across_memory_additions_and_modifications(proto, monkeypatch):
    case = proto
    admitted, pushes, rounds = waiting(case, monkeypatch)
    for index in range(3):
        memory = f".claude/agent-memory/role-{index}.md"
        case.history.update({memory: "memory\n"})
        case.history.update({memory: "updated memory\n"})
    assert_reused(case, admitted, pushes, rounds)


def test_owner_refreshes_for_docs_only_moves(proto, monkeypatch):
    case = proto
    admitted, pushes, rounds = waiting(case, monkeypatch)
    case.history.update({"docs/note.md": "note\n"})
    assert_refreshed(case, admitted, pushes, rounds)
    assert REUSED not in case.store.log_path(5).read_text()


def test_owner_rechecks_docs_used_by_acceptance(proto, monkeypatch):
    case = proto
    case.history.update({"docs/mode.txt": "allow\n", "tests/acceptance/test_docs_input.py":
        "from pathlib import Path\n\ndef test_mode():\n"
        "    assert not Path('src/member_5.py').exists() or "
        "Path('docs/mode.txt').read_text() == 'allow\\n'\n"})
    admitted, pushes, rounds = waiting(case, monkeypatch)
    case.history.update({"docs/mode.txt": "deny\n"})
    support.gate(case)
    assert not case.merges and len(pushes) == 1
    assert pushes[0] != admitted["head"] and len(case.ci.rounds) == rounds + 1
    support.gate(case)
    assert not case.merges and not merge_owner.pending(case.store, 5)
    job = json.loads(merge_owner._path(case.ctx, 5).read_text())
    assert "Acceptance baseline regression" in job["failure"]
    assert REUSED not in case.store.log_path(5).read_text()


@pytest.mark.parametrize("rename", [False, True], ids=["delete", "rename"])
def test_owner_refreshes_when_memory_is_removed(proto, monkeypatch, rename):
    case = proto
    memory = ".claude/agent-memory/role.md"
    case.history.update({memory: "memory\n"})
    admitted, pushes, rounds = waiting(case, monkeypatch)
    change = {memory: None}
    if rename:
        change[".claude/agent-memory/renamed.md"] = "memory\n"
    case.history.update(change)
    assert_refreshed(case, admitted, pushes, rounds)
    assert REUSED not in case.store.log_path(5).read_text()


def test_owner_refreshes_when_the_measured_object_is_unavailable(proto, monkeypatch):
    case = proto
    admitted, pushes, rounds = waiting(case, monkeypatch)
    # A durable SHA in the evidence is not a Git reachability root. Model the
    # recorded commit no longer being available, without deleting Git objects.
    (number, started_at), = merge_owner.admissions(case.store)
    job = {**admitted, "started_at": started_at,
           "comparison": {**admitted["comparison"], "base_sha": "0" * 40}}
    progress = merge_owner._path(case.ctx, number)
    progress.parent.mkdir(parents=True, exist_ok=True)
    progress.write_text(json.dumps(job))
    case.history.update({".claude/agent-memory/role.md": "memory\n"})
    assert_refreshed(case, admitted, pushes, rounds)
    assert REUSED not in case.store.log_path(5).read_text()


def test_owner_refreshes_when_one_intervening_commit_touches_src(proto, monkeypatch):
    case = proto
    admitted, pushes, rounds = waiting(case, monkeypatch)
    case.history.update({".claude/agent-memory/first.md": "memory\n"})
    case.history.update({"src/neighbor.py": "VALUE = 1\n"})
    case.history.update({".claude/agent-memory/role.md": "memory\n"})
    assert_refreshed(case, admitted, pushes, rounds)


def test_owner_refreshes_for_an_added_acceptance_file(proto, monkeypatch):
    case = proto
    admitted, pushes, rounds = waiting(case, monkeypatch)
    locked = "tests/acceptance/test_new_order.py"
    assert locked not in admitted["comparison"]["scope"]["excluded"]
    assert not git_command(case.history.origin, "ls-tree", admitted["comparison"]["base_sha"], "--", locked)
    case.history.update({locked: "def test_new_order():\n    pass\n"})
    body = f"## Files\n- {locked}\n## Done when\n```sh\npytest {locked}\n```\n"
    case.tracker.add_issue(88, "Another open order", body, labels=["approved"])
    case.store.save_approval(88, body=body, body_sha=body_hash(body),
                            acceptance_blobs={locked: git_command(case.history.origin, "rev-parse", f"main:{locked}")},
                            approved_by="human", approved_at=case.clock[0].isoformat())
    assert_refreshed(case, admitted, pushes, rounds)
    assert REUSED not in case.store.log_path(5).read_text()
    assert case.tracker.get_issue(88).state == "open"


def test_owner_refreshes_for_docs_in_local_mode(proto, monkeypatch):
    case = proto
    case.config = replace(case.config, checks="local")
    case.ctx.config = case.config
    admitted, pushes, rounds = waiting(case, monkeypatch)
    assert admitted["comparison"] is None
    case.history.update({"docs/note.md": "note\n"})
    assert_refreshed(case, admitted, pushes, rounds)
    assert REUSED not in case.store.log_path(5).read_text()


def test_owner_refreshes_for_memory_that_the_pr_changed(proto, monkeypatch):
    case = proto
    own = ".claude/agent-memory/shared.md"
    case.history.update({own: "first\n\n\n\nlast\n"})
    model = case.ctx.executor_factory("claude")
    implement = model.run

    def with_memory(**kwargs):
        result = implement(**kwargs)
        (kwargs["worktree"] / own).write_text("PR first\n\n\n\nlast\n")
        return result

    monkeypatch.setattr(model, "run", with_memory)
    admitted, pushes, rounds = waiting(case, monkeypatch)
    assert own in case.tracker.pr_changed_files(admitted["pr"])
    case.history.update({own: "first\n\n\n\nmain last\n"})
    assert_refreshed(case, admitted, pushes, rounds)


def approve_contract(case):
    locked = support.contract.CONTRACT
    body = f"## Files\n- {locked}\n## Done when\n```sh\npytest {locked}\n```\n"
    case.tracker.add_issue(88, "Other contract owner", body, labels=["approved"])
    case.store.save_approval(88, body=body, body_sha=body_hash(body),
                            acceptance_blobs={locked: git_command(case.history.origin, "rev-parse", f"main:{locked}")},
                            approved_by="human", approved_at=case.clock[0].isoformat())


@pytest.mark.parametrize("checks", ["ci", "local"])
def test_modified_acceptance_file_refreshes_even_when_excluded(proto, monkeypatch, checks):
    case = proto
    case.config = replace(case.config, checks=checks)
    case.ctx.config = case.config
    approve_contract(case)
    admitted, pushes, rounds = waiting(case, monkeypatch)
    locked = support.contract.CONTRACT
    if checks == "ci":
        assert locked in admitted["comparison"]["scope"]["excluded"]
    original = git_command(case.history.origin, "show", f"main:{locked}")
    case.history.update({locked: original + "\n# Updated contract\n"})
    approval = case.store.load_approval(88)
    case.store.save_approval(**asdict(replace(
        approval, acceptance_blobs={locked: git_command(case.history.origin, "rev-parse", f"main:{locked}")},
    )))
    assert_refreshed(case, admitted, pushes, rounds)


@pytest.mark.parametrize("diverged", [False, True], ids=["rewound", "non-descendant"])
def test_rewound_or_diverged_base_refreshes_and_checks_lost_passing_test(proto, monkeypatch, diverged):
    case = proto
    case.config = replace(case.config, checks_fallback="local")
    case.ctx.config = case.config
    contract = "tests/acceptance/test_rollback.py"
    case.history.update({"src/rolled_back.py": "VALUE = 1\n", contract:
        "import runpy\nimport pytest\n"
        "@pytest.mark.parametrize('value', [runpy.run_path('src/rolled_back.py')['VALUE']])\n"
        "def test_value(value):\n    assert value > 0\n"})
    ancestor = case.history.tip
    case.history.update({"src/rolled_back.py": "VALUE = 2\n"})
    admitted, pushes, rounds = waiting(case, monkeypatch)
    # Move only the disposable offline origin's ref; no git reset/checkout.
    git_command(case.history.origin, "update-ref", "refs/heads/main", ancestor)
    case.history.tip = ancestor
    if diverged:
        case.history.update({"docs/other-history.md": "another branch\n"})
    support.gate(case)
    assert not case.merges and len(pushes) == 1
    if diverged:
        assert len(case.ci.rounds) == rounds + 1
    else:
        # Merging an ancestor can leave the head unchanged, so the remote has
        # no new CI round. Replace its stale artifacts through normal fallback.
        assert pushes == [admitted["head"]] and len(case.ci.rounds) == rounds

        def unavailable(head):
            raise RejectedEvidence("No new artifacts for an unchanged head")

        monkeypatch.setattr(case.tracker, "pr_acceptance_reports", unavailable)
    assert REUSED not in case.store.log_path(5).read_text()
    support.gate(case)
    assert not case.merges
    job = json.loads(merge_owner._path(case.ctx, 5).read_text())
    assert "test_rollback.py::test_value[1]" in job["failure"]
    assert not merge_owner.pending(case.store, 5)


def test_repeated_neutral_reads_keep_the_original_measurement_base(proto, monkeypatch):
    case = proto
    admitted, pushes, rounds = waiting(case, monkeypatch)
    # Use changing CI status at the final read to keep the admission waiting.
    status = case.tracker.pr_checks
    reads = []

    def pending_on_final(pr):
        reads.append(pr)
        return "pending" if len(reads) == 2 else status(pr)

    monkeypatch.setattr(case.tracker, "pr_checks", pending_on_final)
    case.history.update({".claude/agent-memory/first.md": "first\n"})
    support.gate(case)
    assert not case.merges and not pushes
    job = json.loads(merge_owner._path(case.ctx, 5).read_text())
    assert job["base"] == admitted["base"]
    monkeypatch.setattr(case.tracker, "pr_checks", status)
    case.history.update({".claude/agent-memory/second.md": "second\n"})
    support.gate(case)
    assert [item["head"] for item in case.merges] == [admitted["head"]]
    assert not pushes and len(case.ci.rounds) == rounds
    job = json.loads(merge_owner._path(case.ctx, 5).read_text())
    assert job["merge_base"] != job["base"] == admitted["base"]


def test_refresh_clears_old_merge_intent_before_manual_merge(proto, monkeypatch):
    case = proto
    admitted, pushes, rounds = waiting(case, monkeypatch)
    merge = case.tracker.merge_pr

    def timed_out(*args, **kwargs):
        raise TimeoutError("Merge request timed out before GitHub accepted it")

    monkeypatch.setattr(case.tracker, "merge_pr", timed_out)
    support.gate(case)
    job = json.loads(merge_owner._path(case.ctx, 5).read_text())
    assert job["phase"] == "merging" and job["merge_base"] == admitted["base"]
    assert not case.merges

    monkeypatch.setattr(case.tracker, "merge_pr", merge)
    case.history.update({"src/neighbor.py": "VALUE = 1\n"})
    refreshed_base = case.history.tip
    support.gate(case)
    job = json.loads(merge_owner._path(case.ctx, 5).read_text())
    assert job["head"] == pushes[0] != admitted["head"]
    assert job["base"] == refreshed_base and "merge_base" not in job
    assert not case.merges and len(case.ci.rounds) == rounds + 1

    merge(admitted["pr"], expected_head_sha=job["head"])
    monkeypatch.setattr(case.tracker, "merge_pr", lambda *args, **kwargs: pytest.fail("Duplicate merge"))
    support.gate(case)
    turns = [row for row in support.contract.rows(case)
             if row.get("event") == "turn" and row.get("result") == "merged" and row.get("n") == 5]
    assert len(turns) == 1 and turns[0]["base_sha"] == refreshed_base
    assert not merge_owner.pending(case.store, 5)


def test_replacement_fallback_refreshes_before_measuring_a_neutral_base(proto, monkeypatch):
    case = proto
    case.config = replace(case.config, checks_fallback="local")
    case.ctx.config = case.config
    admitted, pushes, rounds = waiting(case, monkeypatch)
    reports = case.tracker.pr_acceptance_reports
    reads = []

    def disappears(head):
        reads.append(head)
        if len(reads) == 1:
            return reports(head)
        raise RejectedEvidence("Artifacts unavailable after neutral acceptance")

    monkeypatch.setattr(case.tracker, "pr_acceptance_reports", disappears)
    fallback = ci_local.fallback_comparison
    measurements = []

    def measured(attempt, base):
        measurements.append((attempt.git.head_sha(), base))
        return fallback(attempt, base)

    monkeypatch.setattr(ci_local, "fallback_comparison", measured)
    case.history.update({".claude/agent-memory/new-base.md": "note\n"})
    target = case.history.tip
    support.gate(case)  # The locked read notices that artifacts disappeared.
    assert not case.merges and not pushes and not measurements
    job = json.loads(merge_owner._path(case.ctx, 5).read_text())
    assert job["comparison"]["accepted_sha"] == target
    support.gate(case)  # Replacement evidence goes through a refreshed head.
    assert not case.merges and len(pushes) == 1 and not measurements
    assert pushes[0] != admitted["head"]
    support.gate(case)
    assert measurements == [(pushes[0], target)]
    assert [item["head"] for item in case.merges] == pushes
    assert len(case.ci.rounds) == rounds + 1
    job = json.loads(merge_owner._path(case.ctx, 5).read_text())
    assert job["comparison"]["base_sha"] == target
    assert job["comparison"]["accepted_sha"] is None


def test_changed_excluded_contract_with_fallback_cannot_inherit_neutrality(proto, monkeypatch):
    case = proto
    case.config = replace(case.config, checks_fallback="local")
    case.ctx.config = case.config
    approve_contract(case)
    admitted, pushes, rounds = waiting(case, monkeypatch)
    locked = support.contract.CONTRACT
    case.history.update({locked:
        "from pathlib import Path\ndef test_contract():\n"
        "    assert not Path('src/member_5.py').exists()\n"})
    approval = case.store.load_approval(88)
    case.store.save_approval(**asdict(replace(
        approval, acceptance_blobs={locked: git_command(case.history.origin, "rev-parse", f"main:{locked}")},
    )))

    def unavailable(head):
        raise RejectedEvidence("Artifacts unavailable")

    monkeypatch.setattr(case.tracker, "pr_acceptance_reports", unavailable)
    fallback = ci_local.fallback_comparison
    measurements = []

    def withdraw_and_measure(attempt, base):
        measurements.append((attempt.git.head_sha(), base))
        case.store.clear_approval(88)
        return fallback(attempt, base)

    monkeypatch.setattr(ci_local, "fallback_comparison", withdraw_and_measure)
    target = case.history.tip
    support.gate(case)
    assert not case.merges and len(pushes) == 1 and not measurements
    support.gate(case)
    assert measurements == [(pushes[0], target)]
    assert not case.merges and not merge_owner.pending(case.store, 5)
    job = json.loads(merge_owner._path(case.ctx, 5).read_text())
    assert "Acceptance baseline regression" in job["failure"]
    assert REUSED not in case.store.log_path(5).read_text()
    assert pushes[0] != admitted["head"] and len(case.ci.rounds) == rounds + 1
