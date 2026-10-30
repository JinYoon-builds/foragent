"""Acceptance test for night-merge-baseline. Locked by Foragent; do not edit."""

import pytest

from test_premerge_baseline import (
    COMPATIBILITY, PENDING, SUITE, baseline_premerge, premerge_report, update_pr_head,
)
from test_skills_autosync import isolated_home


def test_memory_only_merge_keeps_preexisting_acceptance_failure_nonblocking(baseline_premerge, capsys):
    case, main, pr, head = baseline_premerge
    update_pr_head(case, pr, head, {".claude/agent-memory/fa-order/MEMORY.md": "A useful lesson.\n"})
    code, report = premerge_report(main, pr, capsys)
    assert code == 0 and report["ready"] is True, report
    assert report["broken"] == [] and report["failing_on_base"] == [PENDING], report
    assert main(["orch", "merge", str(pr), "--approved-by", "jin"]) == 0
    assert case.tracker.merged_prs == [pr]


@pytest.mark.parametrize("nonpass", ["skipped", "removed"])
def test_base_passing_acceptance_must_still_pass_on_the_merge_result(baseline_premerge, capsys, nonpass):
    case, main, pr, head = baseline_premerge
    source = (
        "from pathlib import Path\nimport pytest\n\n"
        "def test_other_approved_issue():\n"
        "    assert Path('future-feature.txt').is_file()\n\n"
        "def test_existing_feature():\n"
        "    assert Path('existing-feature.txt').read_text() == 'working\\n'\n\n"
    )
    if nonpass == "skipped":
        source += "@pytest.mark.skip(reason='head stopped verifying compatibility')\ndef test_base_compatibility():\n    assert True\n"
    update_pr_head(case, pr, head, {SUITE: source})
    code, report = premerge_report(main, pr, capsys)
    assert code == 1 and report["ready"] is False, report
    assert COMPATIBILITY in report["broken"], report
    assert report["failing_on_base"] == [PENDING], report
    assert main(["orch", "merge", str(pr), "--approved-by", "jin"]) != 0
    assert case.tracker.merged_prs == []
