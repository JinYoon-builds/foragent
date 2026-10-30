"""Two independent gates see one repo; only the orderer's gate may act."""
from dataclasses import replace
import pytest

from foragent.scheduler import tick
from test_orch_review_handoff import make_case
from test_orch_verbs import blocked_case
from test_orch_stop import _case as stop_case
import test_stop_whole_tree as stop_fakes
from team_v1_support import factory, invoke, legacy, person, rows, snapshot


@pytest.fixture
def fake_processes(monkeypatch, tmp_path, no_forbidden_git_commands):
    # The upstream fixture is autouse in stop tests. Opt in here only for the
    # stop case, since its process ban would mask runner/resolve behavior.
    return stop_fakes.fake_processes.__wrapped__(monkeypatch, tmp_path, no_forbidden_git_commands)


def test_two_factories_admit_disjoint_orders_and_keep_foreign_facts(
    git_history, tmp_path, monkeypatch,
):
    case = make_case(git_history, tmp_path, monkeypatch, max_parallel=1,
                     skill_libraries=[], lanes={"api": {}, "web": {}}, test_command="true")
    legacy(case.ctx.config)
    case.store.finish_run(1, "blocked")
    case.tracker.remove_labels(1, ["running"])
    case.tracker.add_labels(1, ["by:bob", "lane:api"])
    case.tracker.add_issue(2, "Alice's work", case.tracker.get_issue(1).body,
                           labels=["by:alice", "lane:web"])
    for n in (1, 2):
        case.tracker.record_label(n, "approved", "human")
    alice, a = factory(case, tmp_path / "alice", "alice", monkeypatch)
    bob, b = factory(case, tmp_path / "bob", "bob", monkeypatch)
    tick(alice)
    tick(bob)
    assert a == [2] and b == [1], f"Wrong starts: alice={a}, bob={b}"
    assert not set(a) & set(b)
    assert {1, 2} <= rows(alice.store).keys()
    assert {1, 2} <= rows(bob.store).keys()


def test_unlabelled_backlog_stays_with_legacy_factory(git_history, tmp_path, monkeypatch):
    case = make_case(git_history, tmp_path, monkeypatch, skill_libraries=[], test_command="true")
    legacy(case.ctx.config)
    case.store.finish_run(1, "blocked")
    case.tracker.remove_labels(1, ["running"])
    case.tracker.record_label(1, "approved", "human")
    bob, starts = factory(case, tmp_path / "bob", "bob", monkeypatch)
    tick(bob)
    assert starts == [], "A teammate must not adopt the pre-team backlog"
    alice, starts = factory(case, tmp_path / "alice", "alice", monkeypatch)
    tick(alice)
    assert starts == [1], "The original singleton must still work its unlabelled backlog"


def test_single_factory_needs_no_new_settings_for_unlabelled_work(git_history, tmp_path, monkeypatch):
    case = make_case(git_history, tmp_path, monkeypatch, skill_libraries=[], test_command="true")
    case.store.finish_run(1, "blocked")
    case.tracker.remove_labels(1, ["running"])
    case.tracker.record_label(1, "approved", "human")
    ctx, starts = factory(case, tmp_path / "single", "alice", monkeypatch)
    tick(ctx)
    assert starts == [1]
    assert ctx.store.get_run(1).status == "running"


@pytest.mark.parametrize("labels", [["by:bob"], ["by:alice", "by:bob"]])
def test_direct_worker_never_implements_foreign_or_ambiguous_work(
    git_history, tmp_path, monkeypatch, labels,
):
    from foragent.runner import run_issue
    case = make_case(git_history, tmp_path, monkeypatch)
    legacy(case.ctx.config)
    person(case.tracker, monkeypatch, "alice")
    case.tracker.add_labels(1, labels)
    before = case.tracker.get_issue(1)
    try:
        run_issue(1, case.ctx)
    except (ValueError, RuntimeError):
        pass  # A typed refusal or a refusal outcome are both valid public behavior.
    assert case.implementer.calls == [] and case.reviewer.calls == []
    assert case.tracker.merged_prs == []
    assert case.tracker.get_issue(1) == before


def test_foreign_stop_is_a_one_line_refusal_without_any_effect(
    git_history, tmp_path, monkeypatch, fake_processes, capsys,
):
    case, _, main = stop_case(git_history, tmp_path, monkeypatch, fake_processes)
    legacy(case.ctx.config)
    person(case.tracker, monkeypatch, "alice")
    case.tracker.add_labels(1, ["by:bob"])
    before, alive = snapshot(case), set(fake_processes.live)
    code, output = invoke(main, ["orch", "stop", "1", "--keep-work", "--why", "Replan"], capsys)
    assert code != 0 and "bob" in output, output
    assert len(output.strip().splitlines()) == 1, output
    assert snapshot(case) == before
    assert fake_processes.live == alive and not fake_processes.signals


@pytest.mark.parametrize("args", [
    ["retry", "1", "--hint", "Try again"], ["park", "1", "--why", "Wait"],
    ["resolve", "1", "--verdict", "pass", "--note", "Checked"], ["rerun-review", "1"],
])
def test_foreign_mutation_verbs_refuse_before_work_or_writes(
    git_history, tmp_path, monkeypatch, capsys, args,
):
    case, main = blocked_case(git_history, tmp_path, monkeypatch)
    legacy(case.ctx.config)
    person(case.tracker, monkeypatch, "alice")
    case.tracker.add_labels(1, ["by:bob"])
    # Supply a valid existing PR so resolve/review would otherwise be actionable.
    from conftest import git_command
    base = case.history.tip
    case.history.update({"feature.txt": "done"})
    head = case.history.tip
    git_command(case.history.origin, "update-ref", "refs/heads/foragent/issue-1", head)
    git_command(case.history.origin, "update-ref", "refs/heads/main", base)
    case.history.tip = base
    pr = case.tracker.create_pr(head="foragent/issue-1", base="main", title="Feature", body="Review")
    case.tracker.set_pr_head_sha(pr, head)
    case.git.fetch()
    before = snapshot(case)
    code, output = invoke(main, ["orch", *args], capsys)
    assert code != 0 and "bob" in output, output
    assert len(output.strip().splitlines()) == 1
    assert snapshot(case) == before
    assert case.implementer.calls == [] and case.reviewer.calls == []


def test_foreign_outbox_does_not_block_owned_writes(git_history, tmp_path, monkeypatch):
    case = make_case(git_history, tmp_path, monkeypatch)
    legacy(case.ctx.config)
    person(case.tracker, monkeypatch, "alice")
    case.tracker.add_labels(1, ["by:bob"])
    case.tracker.add_issue(2, "Owned work", labels=["by:alice"])
    # These could have been queued by an earlier version or before a label edit.
    foreign = case.tracker.list_comments(1)
    case.store.queue_github_write(1, "comment", body="Old queued write")
    case.store.queue_github_write(2, "comment", body="Owned queued write")
    case.ctx.tracker.flush_outbox()
    assert case.tracker.list_comments(1) == foreign, "Replayed a foreign write"
    assert [c.body for c in case.tracker.list_comments(2)] == ["Owned queued write"]


def test_gate_cleans_its_process_tree_after_owner_edit_without_foreign_writes(
    git_history, tmp_path, monkeypatch, fake_processes,
):
    from datetime import timedelta
    case, tree, _ = stop_case(git_history, tmp_path, monkeypatch, fake_processes)
    legacy(case.ctx.config)
    person(case.tracker, monkeypatch, "alice")
    case.tracker.add_labels(1, ["by:bob"])
    before = snapshot(case)
    protected = fake_processes.live - {row.pid for row in tree}
    case.clock[0] += timedelta(minutes=case.config.max_runtime_minutes + 11)
    tick(stop_fakes.stop_context(case, fake_processes))
    assert not {row.pid for row in tree} & fake_processes.live
    assert protected <= fake_processes.live
    after = snapshot(case)
    assert after["issues"] == before["issues"] and after["comments"] == before["comments"]
    assert after["prs"] == before["prs"] and after["merges"] == before["merges"]


def test_issue_question_mentions_orderer_in_existing_thread(git_history, tmp_path, monkeypatch):
    from foragent.runner import run_issue
    case = make_case(git_history, tmp_path, monkeypatch)
    legacy(case.ctx.config)
    person(case.tracker, monkeypatch, "alice")
    case.tracker.add_labels(1, ["by:alice"])
    def ask(kwargs):
        question = kwargs["worktree"] / ".foragent/QUESTION.md"
        question.parent.mkdir(parents=True, exist_ok=True)
        question.write_text("Which visible behaviour? Keep or change; recommend keep.\n")
    case.implementer.steps = [ask]
    assert run_issue(1, case.ctx) == "spec-question"
    assert any("@alice" in c.body and "Which visible behaviour?" in c.body
               for c in case.tracker.list_comments(1)), "Address the issue question to its orderer"
