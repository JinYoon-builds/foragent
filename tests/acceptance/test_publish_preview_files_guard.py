"""Guard for n97 (passes on main, kept outside Done when): a preview that writes files
keeps one files row naming exactly those paths, in English and Korean."""

from test_publish_preview_files_row import (  # noqa: F401
    ACCEPTANCE, FILES_ROW, REFERENCE, _issue, _plan, _preview, case,
)
from test_order import publishing  # noqa: F401
from test_skills_autosync import isolated_home  # noqa: F401


def test_preview_with_file_outputs_names_exactly_those_paths(case, capsys):
    """Regression control: reject omitting the row when files exist, naming a wrong or
    partial set of paths, or printing the row (or a path) twice."""
    plan = _plan("2026-11-08-with-files", [_issue(
        done_when=[f"uv run pytest {ACCEPTANCE}"],
        acceptance_files={ACCEPTANCE: "def test_export():\n    assert True\n"},
        references=[REFERENCE],
    )], reference_files={REFERENCE: "# Export notes\n"})
    output = _preview(case, capsys, plan)
    rows = [line for line in output.splitlines() if FILES_ROW.match(line)]
    assert len(rows) == 1, f"Exactly one files row expected:\n{output}"
    row = rows[0]
    label = "Files" if case[0] == "en" else "파일"
    assert row.strip().startswith(label), f"The files row must use the {case[0]} label: {row!r}"
    named = {part.strip() for part in row.split(":", 1)[1].split(",") if part.strip()}
    assert named == {ACCEPTANCE, REFERENCE}, f"The files row must name the written paths: {row!r}"
    for path in (ACCEPTANCE, REFERENCE):
        assert row.count(path) == 1, f"{path} must appear once in the files row: {row!r}"
