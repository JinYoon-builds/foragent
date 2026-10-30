"""D85 acceptance: waiting orders/notes. Locked by Foragent; do not edit.

Public controls under test: order publish PLAN --defer BOUNDARY [--urgent],
order note --defer BOUNDARY --kind decision|idea --text TEXT, order pending
--json, and order flush --boundary BOUNDARY. A boundary is explicitly chosen
by the person/agent; no interval, urgency classifier or agent sequence is set.
Only the existing offline tracker, Git adapter and executor fixtures are used.
"""

from copy import deepcopy
import json

import pytest

from foragent.issue import parse_body
from foragent.tracker import TrackerError
from test_order import plan, publishing  # noqa: F401
from test_order_preread import preread, preread_case  # noqa: F401
from test_skills_autosync import cli_exit, isolated_home  # noqa: F401


BOUNDARY = "next-code-merge"
DECISION = "Jin chooses offline operation for the planning controls."
IDEA = "Consider a local preview before a future publication."


def call(case, capsys, *args, ok=True):
    code = cli_exit(case.main, ["order", *map(str, args)])
    output = capsys.readouterr()
    message = output.out + output.err
    if ok:
        assert code == 0, f"Batch publication must support this public control: {message}"
    else:
        assert code != 0, "The injected publication interruption must be reported"
    assert case.gh == [], "The test must use fake publication boundaries, never GitHub"
    return message


def prepare(case, capsys, identity, *, clean=True):
    value = deepcopy(case.plan)
    value["id"] = f"2026-10-31-{identity}"
    value["title"] = f"Order {identity}"
    value["spec_markdown"] = f"# Order {identity}\nPreserve the selected locale.\n"
    for item in value["issues"]:
        item["title"] = f"{identity}: {item['key']}"
        path = f"tests/acceptance/test_{identity}_{item['key']}.py"
        item["acceptance_files"] = {path: (
            "from pathlib import Path\n\ndef test_selected_locale():\n"
            "    assert Path('locale.txt').read_text() == 'en\\n'\n"
        )}
        item["done_when"] = [f"uv run pytest -q {path}"]
        item["checklist"] = []
    case.plan = value
    case.path = case.root / f"{identity}.json"
    case.path.write_text(json.dumps(value), encoding="utf-8")
    if clean:
        preread(case, capsys, *("NONE" for _ in value["issues"]))
    return value, case.path


def pending(case, capsys):
    shown = json.loads(call(case, capsys, "pending", "--json"))
    assert {"orders", "notes"} <= shown.keys(), shown
    return shown


def shared_files(case):
    # Local control state may change; the shared checkout's content must not.
    return {p.relative_to(case.root).as_posix(): p.read_bytes() for p in case.root.rglob("*")
            if p.is_file() and not {".git", ".foragent"}.intersection(p.relative_to(case.root).parts)}


def note(case, capsys, kind, text):
    call(case, capsys, "note", "--defer", BOUNDARY, "--kind", kind, "--text", text)


def children(case, value):
    titles = {item["title"] for item in value["issues"]}
    return {issue.title: issue for issue in case.tracker.list_issues() if issue.title in titles}


def assert_requirements(case, value, *, approved):
    found = children(case, value)
    assert set(found) == {item["title"] for item in value["issues"]}, found
    for item in value["issues"]:
        issue = found[item["title"]]
        assert ("approved" in issue.labels) == approved, issue
        parsed = parse_body(issue.body)
        assert parsed.done_when_commands == item["done_when"]
        assert parsed.out_of_scope == item["out_of_scope"]
        for path, content in item["acceptance_files"].items():
            assert (case.root / path).read_text() == content, "Publish the exact locked test bytes"
    return found


@pytest.fixture
def batch(preread_case, publishing):
    case = preread_case
    case.git_adapter = publishing[3]
    docs = case.root / "docs"
    docs.mkdir(exist_ok=True)
    (docs / "decisions.md").write_text("# Decisions\nExisting decision.\n", encoding="utf-8")
    (docs / "ideas.md").write_text("# Ideas\nExisting idea.\n", encoding="utf-8")
    return case


def test_approved_ordinary_order_waits_durably_without_touching_shared_content(batch, capsys):
    case = batch
    value, path = prepare(case, capsys, "ordinary")
    before = shared_files(case)
    output = call(case, capsys, "publish", path, "--approve", "--defer", BOUNDARY)
    assert "waiting" in output.lower() and BOUNDARY in output, output
    assert case.events == [] and case.tracker.list_issues() == []
    assert shared_files(case) == before
    path.unlink()  # The durable queue must retain content, not only a pathname.
    for _ in range(2):
        rows = pending(case, capsys)["orders"]
        row, = [row for row in rows if row["id"] == value["id"]]
        assert row["status"] == "waiting" and row["boundary"] == BOUNDARY, row
    call(case, capsys, "flush", "--boundary", BOUNDARY)
    assert_requirements(case, value, approved=True)


def test_explicit_urgent_order_publishes_now_without_draining_ordinary_work(batch, capsys):
    case = batch
    ordinary, path = prepare(case, capsys, "ordinary")
    call(case, capsys, "publish", path, "--approve", "--defer", BOUNDARY)
    urgent, path = prepare(case, capsys, "urgent")
    call(case, capsys, "publish", path, "--approve", "--defer", BOUNDARY, "--urgent")
    assert_requirements(case, urgent, approved=True)
    assert children(case, ordinary) == {}
    rows = pending(case, capsys)["orders"]
    assert any(row["id"] == ordinary["id"] and row["status"] == "waiting" for row in rows)
    call(case, capsys, "flush", "--boundary", BOUNDARY)
    assert_requirements(case, ordinary, approved=True)
    assert len(case.tracker.list_issues()) == 6, "Both orders get one parent and their two children"


def test_decisions_and_ideas_wait_outside_the_shared_checkout(batch, capsys):
    case = batch
    before = shared_files(case)
    note(case, capsys, "decision", DECISION)
    note(case, capsys, "idea", IDEA)
    assert shared_files(case) == before
    assert case.events == [] and case.tracker.list_issues() == []
    for _ in range(2):
        rows = pending(case, capsys)["notes"]
        for kind, text in (("decision", DECISION), ("idea", IDEA)):
            assert any(row["kind"] == kind and row["text"] == text
                       and row["status"] == "waiting" and row["boundary"] == BOUNDARY for row in rows), rows
    call(case, capsys, "flush", "--boundary", BOUNDARY)
    assert (case.root / "docs/decisions.md").read_text().count(DECISION) == 1
    assert (case.root / "docs/ideas.md").read_text().count(IDEA) == 1


def test_boundary_publishes_orders_and_notes_together_without_creating_approval(batch, capsys):
    case = batch
    approved, path = prepare(case, capsys, "approved")
    call(case, capsys, "publish", path, "--approve", "--defer", BOUNDARY)
    unapproved, path = prepare(case, capsys, "unapproved")
    call(case, capsys, "publish", path, "--defer", BOUNDARY)
    note(case, capsys, "decision", DECISION)
    note(case, capsys, "idea", IDEA)
    assert case.events == [] and case.tracker.list_issues() == []
    # Another named boundary must not flush this batch by accident.
    call(case, capsys, "flush", "--boundary", "another-publication")
    assert case.events == [] and case.tracker.list_issues() == []
    call(case, capsys, "flush", "--boundary", BOUNDARY)
    assert_requirements(case, approved, approved=True)
    assert_requirements(case, unapproved, approved=False)
    commits = [event for event in case.events if event[0] == "commit"]
    pushes = [event for event in case.events if event[0] == "push"]
    assert len(commits) == len(pushes) == 1, "One chosen boundary publishes the combined file changes"
    published = commits[0][2]
    for value in (approved, unapproved):
        assert f"docs/specs/{value['id']}.md" in published
        for item in value["issues"]:
            assert item["acceptance_files"].items() <= published.items()
    assert DECISION in published["docs/decisions.md"] and IDEA in published["docs/ideas.md"]
    assert "Existing decision." in published["docs/decisions.md"]
    assert "Existing idea." in published["docs/ideas.md"]


def test_waiting_never_bypasses_the_existing_clean_preread_requirement(batch, capsys):
    case = batch
    value, path = prepare(case, capsys, "unchecked", clean=False)
    code = cli_exit(case.main, ["order", "publish", str(path), "--approve", "--defer", BOUNDARY])
    output = capsys.readouterr()
    if code == 0:
        message = call(case, capsys, "flush", "--boundary", BOUNDARY, ok=False)
    else:
        message = output.out + output.err
    assert "preread" in message.lower(), message
    assert case.tracker.list_issues() == [] and case.events == []
    call(case, capsys, "flush", "--boundary", BOUNDARY)
    assert not any("approved" in issue.labels for issue in children(case, value).values())


@pytest.mark.parametrize("interruption", ["push", "issue"])
def test_interrupted_batch_resumes_without_losing_work_or_repeating_effects(
    batch, capsys, monkeypatch, interruption,
):
    case = batch
    first, path = prepare(case, capsys, "first")
    call(case, capsys, "publish", path, "--approve", "--defer", BOUNDARY)
    second, path = prepare(case, capsys, "second")
    call(case, capsys, "publish", path, "--defer", BOUNDARY)
    note(case, capsys, "decision", DECISION)
    note(case, capsys, "idea", IDEA)
    if interruption == "push":
        from foragent.git import GitError

        def fail_push(branch):
            raise GitError("Offline injected push interruption")

        original = case.git_adapter.push_base
        monkeypatch.setattr(case.git_adapter, "push_base", fail_push)
    else:
        original = case.tracker.create_issue
        attempts = []

        def fail_issue(*args, **kwargs):
            attempts.append(args)
            if len(attempts) == 5:  # First order and part of the second already exist.
                raise TrackerError("Offline injected issue interruption")
            return original(*args, **kwargs)

        monkeypatch.setattr(case.tracker, "create_issue", fail_issue)
    output = call(case, capsys, "flush", "--boundary", BOUNDARY, ok=False)
    assert "interruption" in output.lower(), output
    before = {issue.title: issue.number for issue in case.tracker.list_issues()}
    shown = pending(case, capsys)
    assert any(row["status"] != "published" for row in shown["orders"]), shown
    if interruption == "push":
        monkeypatch.setattr(case.git_adapter, "push_base", original)
    else:
        monkeypatch.setattr(case.tracker, "create_issue", original)
    call(case, capsys, "flush", "--boundary", BOUNDARY)
    assert_requirements(case, first, approved=True)
    assert_requirements(case, second, approved=False)
    assert len(case.tracker.list_issues()) == 6
    after = {issue.title: issue.number for issue in case.tracker.list_issues()}
    assert before.items() <= after.items(), "Already created issues keep their identity after resuming"
    assert (case.root / "docs/decisions.md").read_text().count(DECISION) == 1
    assert (case.root / "docs/ideas.md").read_text().count(IDEA) == 1
    assert len([event for event in case.events if event[0] == "commit"]) == 1
    after_events = list(case.events)
    call(case, capsys, "flush", "--boundary", BOUNDARY)
    assert case.events == after_events, "A completed batch must have no publication effects on retry"
    shown = pending(case, capsys)
    assert not [row for row in [*shown["orders"], *shown["notes"]] if row["status"] != "published"]
