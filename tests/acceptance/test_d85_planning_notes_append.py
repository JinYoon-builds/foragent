"""D85 piece 1. Locked acceptance; record notes without publishing them.

Reuse #1043's offline CLI fixtures and public note/pending contract. This
piece deliberately needs no flush, commit, order queue or recovery support.
"""

from pathlib import Path
from types import SimpleNamespace

from conftest import git_command
from test_d85_batched_publication import BOUNDARY, DECISION, IDEA, batch, call, pending, shared_files
from test_local_truth import record_gh
from test_order import offline_publication, plan, publishing  # noqa: F401
from test_order_preread import preread_case  # noqa: F401
from test_publish_out_of_scope import publish_cli
from test_skills_autosync import isolated_home  # noqa: F401


def checkout_files(case):
    """The shared checkout's files; the person's FA state under HOME is not the checkout.

    #1262 (2026-11-01, test owner): the isolated HOME lives inside case.root here, so the
    shared-checkout snapshot excludes that subtree.
    """
    home = Path.home().resolve()
    return {name: data for name, data in shared_files(case).items()
            if not (case.root / name).resolve().is_relative_to(home)}


def retained_files(case, marker):
    """Find the recorded bytes by their content, without fixing a private path."""
    roots = (case.root, Path.home())
    return {path: path.read_bytes() for root in roots for path in root.rglob("*")
            if path.is_file() and ".git" not in path.parts and marker.encode() in path.read_bytes()}


def test_decisions_and_ideas_wait_without_changing_shared_checkout(batch, capsys):
    case = batch
    before = checkout_files(case)
    for kind, text in (("decision", DECISION), ("idea", IDEA)):
        call(case, capsys, "note", "--defer", BOUNDARY, "--kind", kind, "--text", text)
    assert checkout_files(case) == before, "Planning records notes in FA, outside shared content"
    assert case.events == [] and case.tracker.list_issues() == []
    for _ in range(2):  # Separate CLI invocations must see durable notes.
        rows = pending(case, capsys)["notes"]
        for kind, text in (("decision", DECISION), ("idea", IDEA)):
            matches = [row for row in rows if row["kind"] == kind and row["text"] == text]
            assert len(matches) == 1, rows
            assert matches[0]["status"] == "waiting" and matches[0]["boundary"] == BOUNDARY


def test_a_later_append_never_rewrites_or_removes_previous_notes(batch, capsys):
    case = batch
    before = checkout_files(case)
    call(case, capsys, "note", "--defer", BOUNDARY, "--kind", "decision", "--text", DECISION)
    recorded = retained_files(case, DECISION)
    assert recorded, "The completed append must retain the decision durably"
    for text in (IDEA, "A second planning session keeps this later idea."):
        call(case, capsys, "note", "--defer", BOUNDARY, "--kind", "idea", "--text", text)
        for path, original in recorded.items():
            assert path.is_file() and path.read_bytes().startswith(original), (
                "Planning only appends; earlier waiting bytes must remain intact", path)
    rows = pending(case, capsys)["notes"]
    assert [row["text"] for row in rows].count(DECISION) == 1
    assert [row["text"] for row in rows].count(IDEA) == 1
    assert checkout_files(case) == before and case.events == []


def test_order_skill_routes_planning_decisions_and_ideas_through_fa():
    root = next(path for path in Path(__file__).resolve().parents
                if (path / "skills/fa-order/SKILL.md").is_file())
    skill = (root / "skills/fa-order/SKILL.md").read_text()
    assert "fa order note" in skill and "--defer" in skill, (
        "The one-entrance order needs the public FA note path for decision/idea records")
    assert "append-only" in skill.lower(), "Planning must know that it only appends waiting notes"


def test_planning_append_preserves_a_real_checkout_and_its_staged_bytes(
    offline_publication, monkeypatch, capsys,
):
    root, config, tracker, git, _, _ = offline_publication
    (root / "other.txt").write_text("Another session's staged work.\n")
    git_command(root, "add", "--", "other.txt")
    before = git_command(root, "ls-files", "--stage")
    publication = (root, config, tracker, git, [])
    main = publish_cli(publication, monkeypatch)
    monkeypatch.chdir(root)
    case = SimpleNamespace(root=root, main=main, gh=record_gh(monkeypatch))
    shared = checkout_files(case)
    call(case, capsys, "note", "--defer", BOUNDARY, "--kind", "decision", "--text", DECISION)
    call(case, capsys, "note", "--defer", BOUNDARY, "--kind", "idea", "--text", IDEA)
    assert git_command(root, "ls-files", "--stage") == before
    assert checkout_files(case) == shared and tracker.list_issues() == []
