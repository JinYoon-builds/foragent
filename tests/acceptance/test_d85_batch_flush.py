"""D85 piece 3. Locked acceptance; successful publication only, no interruptions."""

from test_d85_batched_publication import (
    BOUNDARY, DECISION, IDEA, assert_requirements, batch, call, note, pending, prepare,
)
from test_order import plan, publishing  # noqa: F401
from test_order_preread import preread, preread_case  # noqa: F401
from test_skills_autosync import isolated_home  # noqa: F401


def test_chosen_boundary_publishes_orders_and_notes_in_one_commit_and_push(batch, capsys):
    case = batch
    approved, path = prepare(case, capsys, "approved")
    call(case, capsys, "publish", path, "--approve", "--defer", BOUNDARY)
    unapproved, path = prepare(case, capsys, "unapproved")
    call(case, capsys, "publish", path, "--defer", BOUNDARY)
    note(case, capsys, "decision", DECISION)
    note(case, capsys, "idea", IDEA)
    path.unlink()  # The caller's draft is no longer needed to flush the order.
    assert case.events == [] and case.tracker.list_issues() == []
    call(case, capsys, "flush", "--boundary", "another-publication")
    assert case.events == [] and case.tracker.list_issues() == []
    call(case, capsys, "flush", "--boundary", BOUNDARY)
    assert_requirements(case, approved, approved=True)
    assert_requirements(case, unapproved, approved=False)
    commits = [event for event in case.events if event[0] == "commit"]
    assert len(commits) == len([event for event in case.events if event[0] == "push"]) == 1
    published = commits[0][2]
    for value in (approved, unapproved):
        assert published[f"docs/specs/{value['id']}.md"] == value["spec_markdown"]
        for item in value["issues"]:
            assert item["acceptance_files"].items() <= published.items()
    assert published["docs/decisions.md"].count(DECISION) == 1
    assert published["docs/ideas.md"].count(IDEA) == 1
    assert "Existing decision." in published["docs/decisions.md"]
    assert "Existing idea." in published["docs/ideas.md"]
    assert len(case.tracker.list_issues()) == 6
    rows = pending(case, capsys)
    assert all(row["status"] == "published" for key in ("orders", "notes") for row in rows[key])
    events = list(case.events)
    call(case, capsys, "flush", "--boundary", BOUNDARY)
    assert case.events == events, "A completed boundary has no duplicate effects"


def test_partial_approval_and_locked_tests_survive_waiting(batch, capsys):
    case = batch
    value, path = prepare(case, capsys, "partial")
    key = value["issues"][0]["key"]
    call(case, capsys, "publish", path, "--approve-keys", key, "--defer", BOUNDARY)
    call(case, capsys, "flush", "--boundary", BOUNDARY)
    found = {issue.title: issue for issue in case.tracker.list_issues()}
    for item in value["issues"]:
        issue = found[item["title"]]
        assert ("approved" in issue.labels) == (item["key"] == key)
        from foragent.issue import parse_body

        assert parse_body(issue.body).done_when_commands == item["done_when"]
        for name, content in item["acceptance_files"].items():
            assert (case.root / name).read_text() == content


def test_rule_note_applies_only_the_agent_supplied_replacement_at_boundary(batch, capsys):
    case = batch
    rules = case.root / "docs/rules.md"
    old = "- Keep the old publication boundary. (D85)"
    new = "- Use the boundary chosen by the person or agent. (D85)"
    rules.write_text(f"# Rules in force\n{old}\n- Keep unrelated rules. (D95)\n")
    call(case, capsys, "note", "--defer", BOUNDARY, "--kind", "rule", "--text", new, "--replace", old)
    assert old in rules.read_text() and new not in rules.read_text()
    call(case, capsys, "flush", "--boundary", BOUNDARY)
    assert old not in rules.read_text() and rules.read_text().count(new) == 1
    assert "- Keep unrelated rules. (D95)" in rules.read_text()
    assert len([event for event in case.events if event[0] == "commit"]) == 1
