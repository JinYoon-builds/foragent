"""A baseline id dropped after one base sync is checked again once main restores it (#123)."""

from dataclasses import replace
from pathlib import Path

from foragent.runner import run_issue

OTHER = "tests/acceptance/test_other_restore.py"


def test_restored_baseline_test_is_verified_again_after_a_later_sync(git_history, tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).parent / "acceptance"))
    from test_acceptance_baseline import KEEP, baseline_case

    git_history.update({
        "other.txt": "other\n",
        OTHER: "from pathlib import Path\n\ndef test_other():\n    assert Path('other.txt').is_file()\n",
    })
    case = baseline_case(git_history, tmp_path, monkeypatch)
    case.ctx.config = replace(case.ctx.config, max_retries=1)

    def implement_while_main_deletes(kwargs):
        case.implement(kwargs)
        # The deleted id is dropped; the removed file fails a surviving test, forcing a retry.
        git_history.update({KEEP: None, "other.txt": None})
        return "Implemented while main deleted test_keep and other.txt"

    def repair_while_main_restores(kwargs):
        (kwargs["worktree"] / "other.txt").write_text("other\n")
        git_history.update({KEEP: "def test_keep():\n    assert False, 'restored and broken'\n"})
        return "Restored other.txt while main restored test_keep"

    case.implementer.steps = [implement_while_main_deletes, repair_while_main_restores]
    case.reviewer.steps = ["VERDICT: PASS", "VERDICT: PASS"]
    outcome = run_issue(1, case.ctx)
    log = case.store.log_path(1).read_text()
    assert outcome != "merged", log
    assert case.tracker.merged_prs == []
    commands = [section.splitlines()[0] for section in log.split("Done when command:\n")[1:]]
    baseline_runs = [command for command in commands
                     if f"{OTHER}::test_other" in command and "--collect-only" not in command]
    assert f"{KEEP}::test_keep" in baseline_runs[-1], baseline_runs
