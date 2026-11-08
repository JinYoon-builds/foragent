"""Acceptance for n97: a publication preview omits an empty files row.

Public contract: ``fa order publish PLAN.json --dry-run`` on a valid plan that writes no
acceptance or reference files succeeds and prints no ``Files:`` (English) or ``파일:``
(Korean) row. A preview that does write files keeps one files row naming exactly those paths.
Only the Git and tracker service boundaries use the existing offline fixtures.
"""

from dataclasses import replace
import json
import re

import pytest

from test_order import publishing  # noqa: F401
from test_publish_out_of_scope import publish_cli
from test_skills_autosync import cli_exit, isolated_home  # noqa: F401


FILES_ROW = re.compile(r"^\s*(Files|파일)\s*:", re.MULTILINE)
ACCEPTANCE = "tests/acceptance/test_export_button.py"
REFERENCE = "docs/design/export/notes.md"


def _issue(key="work", **extra):
    issue = {
        "key": key, "title": f"Add the export button {key}", "goal": "Add the button.",
        "out_of_scope": "Other work.", "done_when": ["true"], "lane": "api", "size": "s",
        "blocked_by": [], "acceptance_files": {},
    }
    issue.update(extra)
    return issue


def _plan(plan_id, issues, **extra):
    plan = {"id": plan_id, "title": "Export order", "summary": "Add exporting.",
            "spec_markdown": "# Export\n", "issues": issues}
    plan.update(extra)
    return plan


@pytest.fixture(params=["en", "ko"])
def case(request, publishing, monkeypatch):
    root, config, tracker, git, events = publishing
    config = replace(config, language=request.param, executor="codex", skill_libraries=[])
    monkeypatch.chdir(root)
    main = publish_cli((root, config, tracker, git, events), monkeypatch)
    return request.param, root, tracker, events, main


def _preview(case, capsys, plan):
    language, root, tracker, events, main = case
    path = root / "PLAN.json"
    path.write_text(json.dumps(plan), encoding="utf-8")
    capsys.readouterr()
    before = dict(tracker.issues)
    code = cli_exit(main, ["order", "publish", str(path), "--repo-root", str(root), "--dry-run"])
    captured = capsys.readouterr()
    output = captured.out + captured.err
    assert code == 0, output
    assert dict(tracker.issues) == before and events == [], "A dry run must not publish anything"
    assert "Traceback" not in output, output
    return output


EMPTY_PLANS = {
    # The statement's case: the publication would create only the parent issue.
    "parent-only": lambda: _plan("2026-11-08-no-files-parent", []),
    "one-issue": lambda: _plan("2026-11-08-no-files", [_issue()]),
    "two-issues": lambda: _plan("2026-11-08-no-files-two", [_issue("a"), _issue("b")]),
    "empty-references": lambda: _plan("2026-11-08-no-files-refs", [_issue()], reference_files={}),
}


@pytest.mark.parametrize("shape", sorted(EMPTY_PLANS))
def test_preview_without_file_outputs_has_no_files_row(case, capsys, shape):
    """Reject printing an empty ``Files:``/``파일:`` row when nothing would be written, and
    a fix that drops the rest of the preview (heading and issue rows) with it."""
    plan = EMPTY_PLANS[shape]()
    output = _preview(case, capsys, plan)
    assert not FILES_ROW.search(output), (
        f"A preview with no file outputs must not print a files row:\n{output}"
    )
    assert plan["title"] in output, f"The preview heading must remain:\n{output}"
    for issue in plan["issues"]:
        assert issue["title"] in output, f"Each issue row must remain in the preview:\n{output}"
    if plan["issues"]:
        assert "lane:api" in output, output
