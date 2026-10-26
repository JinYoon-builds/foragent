"""Acceptance test for baseline-renamed-tests. Locked by Foragent; do not edit."""

from dataclasses import replace
import shlex
import sys

import pytest

from conftest import git_command
from foragent.runner import run_issue
from test_acceptance_baseline import KEEP, baseline_case, make_case


KEEP_NODE = f"{KEEP}::test_keep"
RENAMED = "tests/acceptance/test_renamed_keep.py"


def assert_merged(case):
    outcome = run_issue(1, case.ctx)
    log = case.store.log_path(1).read_text()
    assert outcome == "merged", f"run_issue returned {outcome}\n{log}"
    assert len(case.tracker.merged_prs) == 1
    assert not case.tracker.get_issue(1).labels & {"blocked", "needs-human"}
    # Prove that the published tree includes main's change, rather than merely
    # succeeding against the old tree where every baseline node still exists.
    git_command(case.history.origin, "merge-base", "--is-ancestor", case.history.tip,
                "refs/heads/foragent/issue-1")
    return log


def assert_dropped(log, node_id):
    lines = log.splitlines()
    assert any(
        node_id in line and any(
            word in "\n".join(lines[max(0, index - 2):index + 3]).lower()
            for word in ("dropped", "missing")
        )
        for index, line in enumerate(lines)
    ), f"No dropped/missing audit entry for {node_id}\n{log}"


@pytest.mark.parametrize("change", ["file-rename", "function-rename", "deletion"])
def test_baseline_renamed_tests_missing_node_is_dropped_after_main_moves(
    git_history, tmp_path, monkeypatch, change,
):
    case = baseline_case(git_history, tmp_path, monkeypatch)
    case.ctx.config = replace(case.ctx.config, max_retries=0)
    source = (case.root / KEEP).read_text()
    changes = {KEEP: None}
    if change == "file-rename":
        changes[RENAMED] = source
    elif change == "function-rename":
        changes[KEEP] = source.replace("def test_keep():", "def test_kept():")

    def implement_and_move_main(kwargs):
        case.implement(kwargs)
        git_history.update(changes)
        return "Implemented while another issue changed main's acceptance tests"

    case.implementer.steps = [implement_and_move_main]
    log = assert_merged(case)
    assert len(case.implementer.calls) == 1
    prompt = case.implementer.calls[0]["prompt"]
    assert "already pass" in prompt and KEEP_NODE in prompt
    assert_dropped(log, KEEP_NODE)


def test_baseline_renamed_tests_surviving_failure_still_returns_to_implementer(
    git_history, tmp_path, monkeypatch,
):
    obsolete = "tests/acceptance/test_obsolete.py"
    renamed = "tests/acceptance/test_relocated.py"
    source = "def test_existing():\n    assert True\n"
    git_history.update({obsolete: source})
    case = baseline_case(git_history, tmp_path, monkeypatch)
    case.ctx.config = replace(case.ctx.config, max_retries=1)

    def implement_and_move_main(kwargs):
        case.implement(kwargs)
        # The branch breaks test_keep while main relocates a test through the
        # ordinary base merge. test_keep still passes on the new base, so it
        # stays protected; the missing selector must not hide its failure.
        (kwargs["worktree"] / "keep.txt").unlink()
        git_history.update({obsolete: None, renamed: source})
        return "Implemented and removed keep.txt while main relocated a test"

    def restore_keep(kwargs):
        (kwargs["worktree"] / "keep.txt").write_text("keep\n")
        return "Restored the behavior required by the surviving baseline test"

    case.implementer.steps = [implement_and_move_main, restore_keep]
    case.reviewer.steps = ["VERDICT: PASS", "VERDICT: PASS"]
    log = assert_merged(case)
    assert len(case.implementer.calls) == 2
    retry_prompt = case.implementer.calls[1]["prompt"]
    assert KEEP_NODE in retry_prompt and "AssertionError" in retry_prompt
    assert "ERROR: not found" not in retry_prompt
    assert "test_future" not in retry_prompt
    assert_dropped(log, f"{obsolete}::test_existing")


def test_baseline_renamed_tests_all_nodes_vanished_skips_verification(
    git_history, tmp_path, monkeypatch,
):
    acceptance_dir = "checks/acceptance"
    vanished = [f"{acceptance_dir}/test_first.py", f"{acceptance_dir}/test_second.py"]
    source = "def test_existing():\n    assert True\n"
    git_history.update({
        **{path: source for path in vanished},
        f"{acceptance_dir}/test_future.py": (
            "def test_future():\n    assert False, 'not implemented on the base'\n"
        ),
    })
    case = make_case(git_history, tmp_path, monkeypatch, acceptance_dir=acceptance_dir,
                     test_command=f"{shlex.quote(sys.executable)} -m pytest -q", max_retries=0)
    before_sync = []

    def implement_and_move_main(kwargs):
        case.implement(kwargs)
        git_history.update({path: None for path in vanished})
        return "Implemented while main deleted every passing baseline test"

    def review_before_sync(kwargs):
        # Review follows the first verification and precedes the normal base
        # refresh. Later log commands therefore belong to the updated tree.
        before_sync.append(len(case.store.log_path(1).read_text()))
        return "VERDICT: PASS"

    case.implementer.steps = [implement_and_move_main]
    case.reviewer.steps = [review_before_sync]
    log = assert_merged(case)
    assert len(case.implementer.calls) == 1 and len(before_sync) == 1
    prompt = case.implementer.calls[0]["prompt"]
    assert "already pass" in prompt
    for path in vanished:
        node_id = f"{path}::test_existing"
        assert node_id in prompt
        assert_dropped(log, node_id)
    assert "test_future" not in prompt

    commands = [section.splitlines()[0]
                for section in log[before_sync[0]:].split("Done when command:\n")[1:]]
    executions = [command for command in commands
                  if "pytest" in shlex.split(command)
                  and not {"--collect-only", "--co"}.intersection(shlex.split(command))]
    assert executions == [], f"Empty baseline must skip test execution: {executions}"
