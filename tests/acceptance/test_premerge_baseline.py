"""Acceptance test for premerge-baseline. Locked by Foragent; do not edit."""

import pytest

from test_skills_autosync import isolated_home


SUITE = "tests/acceptance/test_premerge_example.py"
PENDING = f"{SUITE}::test_other_approved_issue"
EXISTING = f"{SUITE}::test_existing_feature"
COMPATIBILITY = f"{SUITE}::test_base_compatibility"
ADDED = "tests/acceptance/test_added_by_pr.py"
NEW_FAILURE = f"{ADDED}::test_new_feature"


@pytest.fixture
def baseline_premerge(git_history, tmp_path, monkeypatch, isolated_home):
    """Real pytest suites and local Git history; only external services are faked."""
    from dataclasses import replace
    import shlex
    import sys
    import tempfile

    from test_local_truth import record_gh
    from test_orch_merge import premerge_case

    calls = record_gh(monkeypatch)
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    git_history.update({
        SUITE: (
            '"""Acceptance test for premerge-example. Locked by Foragent; do not edit."""\n'
            "from pathlib import Path\n\n"
            "def test_other_approved_issue():\n"
            "    assert Path('future-feature.txt').is_file(), 'Another approved issue is still unimplemented'\n\n"
            "def test_existing_feature():\n"
            "    assert Path('existing-feature.txt').read_text() == 'working\\n'\n\n"
            "def test_base_compatibility():\n"
            "    required = Path('required-version.txt').read_text().strip()\n"
            "    assert required in Path('supported-versions.txt').read_text().splitlines()\n"
        ),
        "existing-feature.txt": "working\n",
        "required-version.txt": "v1\n",
        "supported-versions.txt": "v1\nv2\n",
    })
    case, main, pr, head = premerge_case(git_history, tmp_path, monkeypatch)
    case.tracker.add_issue(99, "Implement the pending feature", labels=["approved"])
    case.ctx.config = replace(
        case.ctx.config,
        test_command=f"{shlex.quote(sys.executable)} -m pytest -q -p no:cacheprovider",
    )
    yield case, main, pr, head
    assert calls == [], "Premerge acceptance cases must never call real GitHub"


def update_pr_head(case, pr, head, files):
    """Advance just the local PR branch without changing its current base."""
    from conftest import git_command

    base = case.history.tip
    git_command(case.history.origin, "update-ref", "refs/heads/main", head)
    case.history.tip = head
    case.history.update(files)
    updated_head = case.history.tip
    git_command(case.history.origin, "update-ref", "refs/heads/foragent/issue-1", updated_head)
    git_command(case.history.origin, "update-ref", "refs/heads/main", base)
    case.history.tip = base
    case.tracker.set_pr_head_sha(pr, updated_head)
    return updated_head


def premerge_report(main, pr, capsys):
    import json

    result = main(["orch", "premerge", str(pr), "--json"])
    assert result in (0, 1)
    report = json.loads(capsys.readouterr().out)
    assert report["pr"] == pr and report["ci"] == "pass"
    assert report["conflicts"] == []
    return result, report


def assert_merge_refused(case, main, pr):
    assert main(["orch", "merge", str(pr), "--approved-by", "jin"]) != 0
    assert case.tracker.merged_prs == []
    assert not any("Approved by" in comment.body for comment in case.tracker.list_comments(pr))


def test_premerge_baseline_ignores_failures_already_on_base(baseline_premerge, capsys):
    case, main, pr, _ = baseline_premerge

    result, report = premerge_report(main, pr, capsys)

    assert report["tests"] == "pass", report
    assert result == 0 and report["ready"] is True
    assert report.get("failing_on_base") == [PENDING], report
    assert report.get("broken") == [], report
    assert case.tracker.merged_prs == []
    assert case.tracker.list_comments(pr) == []


def test_premerge_baseline_merge_allows_only_preexisting_failures(baseline_premerge):
    case, main, pr, head = baseline_premerge

    result = main(["orch", "merge", str(pr), "--approved-by", "jin"])

    assert result == 0, "An unrelated acceptance failure on the base must not block an approved merge"
    assert case.tracker.merged_prs == [pr]
    assert case.tracker.pr_head_sha(pr) == head


def test_premerge_baseline_reports_regression_and_refuses_merge(baseline_premerge, capsys):
    case, main, pr, head = baseline_premerge
    update_pr_head(case, pr, head, {"existing-feature.txt": "broken\n"})

    result, report = premerge_report(main, pr, capsys)

    assert result == 1 and report["tests"] == "fail" and report["ready"] is False
    assert_merge_refused(case, main, pr)
    # A passing and a failing node share a file: excluding the whole file loses the regression.
    assert report.get("broken") == [EXISTING], report
    assert report.get("failing_on_base") == [PENDING], report


def test_premerge_baseline_new_failing_test_is_broken(baseline_premerge, capsys):
    case, main, pr, head = baseline_premerge
    update_pr_head(case, pr, head, {
        ADDED: (
            '"""Acceptance test for added-by-pr. Locked by Foragent; do not edit."""\n'
            "from pathlib import Path\n\n"
            "def test_new_feature():\n"
            "    assert Path('new-feature.txt').is_file()\n"
        ),
    })

    result, report = premerge_report(main, pr, capsys)

    assert result == 1 and report["tests"] == "fail" and report["ready"] is False
    assert_merge_refused(case, main, pr)
    assert report.get("broken") == [NEW_FAILURE], report
    assert report.get("failing_on_base") == [PENDING], report


def test_premerge_baseline_checks_head_merged_with_current_base(baseline_premerge, capsys):
    case, main, pr, head = baseline_premerge
    # Head alone still needs v1; base alone still supports v2. Only their merge breaks compatibility.
    update_pr_head(case, pr, head, {
        "supported-versions.txt": "v1\n", "future-feature.txt": "implemented\n",
    })
    case.history.update({"required-version.txt": "v2\n", "future-feature.txt": "implemented\n"})

    result, report = premerge_report(main, pr, capsys)

    assert report["tests"] == "fail" and report["ready"] is False, report
    assert result == 1
    assert_merge_refused(case, main, pr)
    assert report.get("broken") == [COMPATIBILITY], report
    assert report.get("failing_on_base") == [], report
