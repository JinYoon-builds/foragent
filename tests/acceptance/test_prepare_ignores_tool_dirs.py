"""Prepare's public scratch comparison ignores empty folders but still refuses files.

Use real temporary Git repositories and order_scratch.build/changes, the same entry
points prepare uses. No writer, reviewer, publication or machine test slot is needed.
Refusal guards check the offending file; ignoring an unrelated empty directory is
covered separately by the missing-behaviour tests.
"""

from types import SimpleNamespace

import pytest

from foragent import order_scratch
from foragent.git import GitRepo


TEST_PATH = "tests/acceptance/test_writer_result.py"
CANDIDATE = b"def test_writer_result():\n    assert False, 'not implemented'\n"
EMPTY_FOLDERS = [
    (".claude/.cc-writes",),
    ("tool-state/a/b",),
    (".claude/.cc-writes", "tool-state/a/b"),
]


@pytest.fixture
def scratch(git_history, tmp_path):
    git = GitRepo(git_history.clone)
    tree = tmp_path / "writer"
    manifest = order_scratch.build(git, git_history.tip, tree)
    before = order_scratch.stamps(tree, manifest)
    candidate = tree / TEST_PATH
    candidate.parent.mkdir(parents=True, exist_ok=True)
    candidate.write_bytes(CANDIDATE)
    return SimpleNamespace(git=git, tree=tree, manifest=manifest, before=before)


def changes(case):
    return order_scratch.changes(case.git, case.tree, case.manifest, "tests/acceptance", before=case.before)


def make_folders(case, folders):
    for folder in folders:
        (case.tree / folder).mkdir(parents=True, exist_ok=True)


def test_acceptance_file_without_tool_folders_is_handed_over(scratch):
    assert changes(scratch) == ({TEST_PATH: CANDIDATE}, [], [])


@pytest.mark.parametrize("folders", EMPTY_FOLDERS, ids=lambda f: "+".join(f))
def test_empty_new_folder_outside_acceptance_is_as_if_absent(scratch, folders):
    baseline = changes(scratch)
    assert baseline == ({TEST_PATH: CANDIDATE}, [], [])
    make_folders(scratch, folders)
    assert changes(scratch) == baseline, "Empty folders must not change prepare's handover or refusals"
    assert all((scratch.tree / folder).is_dir() for folder in folders), "Comparison leaves the writer's tree intact"


@pytest.mark.parametrize("outside", [".claude/notes.md", "docs/extra.md", "tool-state/a/b/out.txt"])
def test_file_outside_acceptance_is_refused_beside_an_empty_tool_folder(scratch, outside):
    make_folders(scratch, (".claude/.cc-writes",))
    target = scratch.tree / outside
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("written outside the acceptance directory\n", encoding="utf-8")
    candidates, refused, ignored = changes(scratch)
    assert refused.count(outside) == 1, f"The outside file must be refused once: {refused}"
    assert candidates == {TEST_PATH: CANDIDATE}, "Only the acceptance file may be handed over"
    assert ignored == [], "No ignore rule exempts these paths"
    assert target.read_text(encoding="utf-8") == "written outside the acceptance directory\n"


def test_tool_folder_that_holds_a_file_is_refused(scratch):
    make_folders(scratch, ("tool-state/empty", ".claude/.cc-writes"))
    outside = ".claude/.cc-writes/record.json"
    (scratch.tree / outside).write_text("{}\n", encoding="utf-8")
    candidates, refused, ignored = changes(scratch)
    assert refused.count(outside) == 1, f"A tool folder's file must be refused once: {refused}"
    assert candidates == {TEST_PATH: CANDIDATE}
    assert ignored == []


def test_folder_holding_only_a_nested_file_is_refused(scratch):
    make_folders(scratch, ("tool-state/a/b",))
    outside = "tool-state/a/b/c.bin"
    (scratch.tree / outside).write_bytes(b"\0data")
    candidates, refused, ignored = changes(scratch)
    assert refused.count(outside) == 1, f"A nested file must be refused once: {refused}"
    assert candidates == {TEST_PATH: CANDIDATE}
    assert ignored == []
