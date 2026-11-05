"""Orders keep their spec in the parent issue, with no per-order spec files.

Exercise the public CLI with the existing offline Git/tracker/executor boundaries.
Missing behaviour and lifecycle guards are separate so main's intended failures do
not hide regressions in publishing, flushing, approval or recovery. Definition-only
function removal is internal cleanup, not an observable acceptance contract.
"""

from collections import Counter
from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from foragent.config import load_config
from foragent.git import GitError
from foragent.issue import parse_body
from foragent.tracker import TrackerError

from test_local_truth import record_gh
from test_order import plan, publishing  # noqa: F401
from test_publish_out_of_scope import publish_cli
from test_publish_own_paths import with_clean_preread
from test_skills_autosync import cli_exit, isolated_home  # noqa: F401


ROUTES = ("publish", "flush", "recover-push", "recover-issue")
BOUNDARY = "approved-login-batch"
DECISION = "Keep the login response in the selected locale."
EXISTING_DECISIONS = "# Decisions\nExisting project decision.\n"
SPEC = (
    "# Offline login contract\n\n"
    "Accept the selected locale without contacting a remote service.\n\n"
    "## Response\n"
    "Return the exact UTF-8 message `로그인 완료` once.\n\n"
    "```json\n{\"locale\": \"ko\", \"status\": \"accepted\"}\n```\n"
)
REPOSITORY = Path(__file__).resolve().parents[2]


@pytest.fixture
def spec_case(publishing, plan, monkeypatch):
    root, config, tracker, git, events = publishing
    config = replace(config, language="en", skill_libraries=[])
    plan.update(id="2026-11-05-no-spec-files", title="Publish login without spec files",
                summary="Publish the agreed login work.", spec_markdown=SPEC)
    for item in plan["issues"]:
        name = f"tests/acceptance/test_no_spec_{item['key']}.py"
        item["acceptance_files"] = {name: (
            "from pathlib import Path\n\n"
            f"def test_{item['key']}_response():\n"
            "    assert Path('login-response.txt').read_text() == 'accepted\\n'\n"
        )}
        item["done_when"] = [f"uv run pytest -q {name}"]
    path = root / "PLAN.json"
    path.write_text(json.dumps(plan), encoding="utf-8")
    docs = root / "docs"
    docs.mkdir()
    (docs / "decisions.md").write_text(EXISTING_DECISIONS, encoding="utf-8")
    (docs / "guide.md").write_text("# Project guide\nKeep this documentation.\n", encoding="utf-8")
    calls = []
    # Observe native GitHub writes, including repeats the fake's sets would hide.
    for name in ("add_sub_issue", "add_blocked_by"):
        original = getattr(tracker, name)

        def record(*args, _name=name, _original=original):
            calls.append((_name, *args))
            return _original(*args)

        monkeypatch.setattr(tracker, name, record)
    main = with_clean_preread(
        publish_cli((root, config, tracker, git, events), monkeypatch), monkeypatch,
    )
    return SimpleNamespace(root=root, config=config, tracker=tracker, git=git,
                           events=events, calls=calls, plan=plan, path=path,
                           main=main, gh=record_gh(monkeypatch))


def call(case, capsys, *args, ok=True):
    capsys.readouterr()
    code = cli_exit(case.main, ["order", *map(str, args), "--repo-root", str(case.root)])
    captured = capsys.readouterr()
    message = captured.out + captured.err
    if ok:
        assert code == 0, message
    else:
        assert code != 0, "The injected publication interruption must be reported"
    assert case.gh == [], "All GitHub operations must use the offline tracker"
    return message


def publish_order(case, capsys, monkeypatch, route):
    """Complete one order, optionally resuming a partially published batch."""
    if route == "publish":
        call(case, capsys, "publish", case.path, "--approve")
        return
    call(case, capsys, "publish", case.path, "--approve", "--defer", BOUNDARY)
    assert case.events == [] and case.tracker.list_issues() == []
    assert not (case.root / "docs/specs").exists()
    case.path.unlink()  # Flush/recovery must use retained content, not the draft.
    call(case, capsys, "note", "--defer", BOUNDARY, "--kind", "decision", "--text", DECISION)
    before = {}
    if route.startswith("recover-"):
        with monkeypatch.context() as patch:
            if route == "recover-push":
                def interrupt_push(branch):
                    raise GitError("Injected publication interruption before push")

                patch.setattr(case.git, "push_base", interrupt_push)
            else:
                create = case.tracker.create_issue
                attempts = []

                def interrupt_issue(*args, **kwargs):
                    attempts.append(args)
                    if len(attempts) == 3:  # Parent and first child already exist.
                        raise TrackerError("Injected publication interruption during issues")
                    return create(*args, **kwargs)

                patch.setattr(case.tracker, "create_issue", interrupt_issue)
            message = call(case, capsys, "flush", "--boundary", BOUNDARY, ok=False)
            assert "Injected publication interruption" in message, message
            assert Counter(event[0] for event in case.events) == (
                Counter(commit=1) if route == "recover-push"
                else Counter(commit=1, push=1, issue=2)
            )
            before = {issue.title: issue.number for issue in case.tracker.list_issues()}
    result = json.loads(call(case, capsys, "flush", "--boundary", BOUNDARY, "--json"))
    assert result["status"] == "published", result
    after = {issue.title: issue.number for issue in case.tracker.list_issues()}
    assert before.items() <= after.items(), "Recovery must reuse the issues already created"
    pending = json.loads(call(case, capsys, "pending", "--json"))
    assert [(row["id"], row["status"]) for row in pending["orders"]] == [
        (case.plan["id"], "published"),
    ]
    assert [(row["text"], row["status"]) for row in pending["notes"]] == [(DECISION, "published")]
    events, calls = list(case.events), list(case.calls)
    issues = case.tracker.list_issues()
    decisions = (case.root / "docs/decisions.md").read_bytes()
    call(case, capsys, "flush", "--boundary", BOUNDARY)
    assert case.events == events and case.calls == calls, "A completed flush must repeat no effects"
    assert case.tracker.list_issues() == issues
    assert (case.root / "docs/decisions.md").read_bytes() == decisions


def parent_and_children(case):
    issues = case.tracker.list_issues()
    assert len(issues) == 3, "Publish exactly one parent and the two agreed children"
    parents = [issue for issue in issues if issue.title == case.plan["title"]]
    assert len(parents) == 1
    children = {item["key"]: [issue for issue in issues if issue.title == item["title"]]
                for item in case.plan["issues"]}
    assert {key: len(found) for key, found in children.items()} == {"api": 1, "client": 1}
    return parents[0], {key: found[0] for key, found in children.items()}


@pytest.mark.parametrize("route", ROUTES)
def test_publication_writes_no_spec_files(spec_case, capsys, monkeypatch, route):
    """Reject spec files from immediate, queued and recovered publication paths."""
    case = spec_case
    publish_order(case, capsys, monkeypatch, route)
    files = sorted(p.relative_to(case.root).as_posix()
                   for p in (case.root / "docs/specs").rglob("*") if p.is_file())
    assert files == [], f"The parent issue owns the spec; publication still wrote {files}"
    assert not (case.root / "docs/specs").exists(), "Publication must not create even an empty specs directory"
    committed_specs = [name for event in case.events if event[0] == "commit"
                       for name in event[2] if name.startswith("docs/specs/")]
    assert committed_specs == [], "Spec files must not enter the published Git files"
    expected = {name: content for item in case.plan["issues"]
                for name, content in item["acceptance_files"].items()}
    if route != "publish":
        expected["docs/decisions.md"] = EXISTING_DECISIONS + f"- {DECISION}\n"
    assert [event[2] for event in case.events if event[0] == "commit"] == [expected], (
        "Publish exactly the agreed tests/notes; relocating a per-order spec is not removal"
    )


@pytest.mark.parametrize("route", ROUTES)
def test_parent_issue_contains_the_complete_spec_once(spec_case, capsys, monkeypatch, route):
    """Reject dropping the spec, retaining only its heading, altering it or appending it twice."""
    case = spec_case
    publish_order(case, capsys, monkeypatch, route)
    parent, _children = parent_and_children(case)
    assert parent.body.count(SPEC) == 1, (
        "The parent issue body must contain the complete spec_markdown exactly once; "
        "a summary or a link to a spec file cannot replace it"
    )
    if route == "publish":
        revised = SPEC.replace("without contacting a remote service", "using only the local cache")
        case.plan["spec_markdown"] = revised
        case.path.write_text(json.dumps(case.plan), encoding="utf-8")
        call(case, capsys, "publish", case.path, "--approve")
        updated, _children = parent_and_children(case)
        assert updated.number == parent.number
        assert updated.body.count(revised) == 1 and updated.body.count(SPEC) == 0
        assert Counter(event[1] for event in case.events if event[0] == "issue") == Counter(
            [case.plan["title"], *(item["title"] for item in case.plan["issues"])])


def test_planning_context_does_not_index_legacy_spec_headings(spec_case, capsys):
    """Leftover files in a project must not reintroduce the retired spec index."""
    case = spec_case
    directory = case.root / "docs/specs"
    directory.mkdir()
    headings = ("Legacy offline contract alpha", "Legacy offline contract beta")
    for index, heading in enumerate(headings):
        (directory / f"old-{index}.md").write_text(
            f"Preamble\n## {heading}\nOld body {index}.\n", encoding="utf-8",
        )
    output = call(case, capsys, "context", "--json")
    context = json.loads(output)
    assert context["language"] == "en" and context["acceptance_dir"] == case.config.acceptance_dir
    assert context.get("specs", []) == [], "Planning context must stop listing the docs/specs index"
    assert [output.count(heading) for heading in headings] == [0, 0]
    assert case.events == [] and case.tracker.list_issues() == []


@pytest.mark.parametrize("directory", ("docs/specs", "docs/impl"))
def test_retired_document_directories_are_absent_from_repository(directory):
    assert not (REPOSITORY / directory).exists(), f"Remove the retired {directory} directory from the repository"


def test_legacy_merge_turn_size_project_config_still_loads_guard(tmp_path):
    """Removing an unused setting must not reject an existing valid project file."""
    path = tmp_path / ".foragent.yml"
    original = "merge_turn_size: 1\nlanguage: en\nbase_branch: trunk\nlanes: {}\n"
    path.write_text(original, encoding="utf-8")
    config = load_config(tmp_path)
    assert (config.language, config.base_branch, config.lanes) == ("en", "trunk", {})
    assert path.read_text(encoding="utf-8") == original


@pytest.mark.parametrize("route", ROUTES)
def test_order_publication_and_recovery_guard(spec_case, capsys, monkeypatch, route):
    """Keep exact file bytes, issue identities, approvals and native dependency writes."""
    case = spec_case
    publish_order(case, capsys, monkeypatch, route)
    parent, children = parent_and_children(case)
    assert Counter(event[0] for event in case.events) == Counter(commit=1, push=1, issue=3)
    assert Counter(event[1] for event in case.events if event[0] == "issue") == Counter(
        [case.plan["title"], *(item["title"] for item in case.plan["issues"])])
    assert parent.body.count(case.plan["summary"]) == 1
    assert case.tracker.parent_of(parent.number) is None
    assert sorted(issue.number for issue in case.tracker.sub_issues(parent.number)) == sorted(
        child.number for child in children.values())
    assert Counter(case.calls) == Counter([
        ("add_sub_issue", parent.number, children["api"].number),
        ("add_sub_issue", parent.number, children["client"].number),
        ("add_blocked_by", children["client"].number, children["api"].number),
    ])
    committed, = [event[2] for event in case.events if event[0] == "commit"]
    for item in case.plan["issues"]:
        child = children[item["key"]]
        assert child.state == "open" and "approved" in child.labels
        assert f"size:{item['size']}" in child.labels
        assert case.tracker.parent_of(child.number) == parent.number
        parsed = parse_body(child.body)
        assert (parsed.goal, parsed.out_of_scope, parsed.done_when_commands) == (
            item["goal"], item["out_of_scope"], item["done_when"],
        )
        assert [dep.number for dep in case.tracker.blocked_by(child.number)] == [
            children[key].number for key in item["blocked_by"]]
        for name, content in item["acceptance_files"].items():
            assert (case.root / name).read_bytes() == content.encode("utf-8")
            assert committed[name] == content and case.git.saved[name] == content
    assert (case.root / "docs/guide.md").read_text() == "# Project guide\nKeep this documentation.\n"
    expected = EXISTING_DECISIONS + (f"- {DECISION}\n" if route != "publish" else "")
    assert (case.root / "docs/decisions.md").read_text() == expected
    if route != "publish":
        assert committed["docs/decisions.md"] == expected
