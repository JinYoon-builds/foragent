"""Acceptance for edit-keeps-lines. Locked by Foragent; do not edit.

Public contracts added by this order:
* edits[].done_when_removals: [{"line": exact command or checklist text,
                              "reason": nonempty explanation}].
* fa order edit NUMBER --json emits an edit seeded from the current issue.

Only tracker, Git and executor service boundaries use fakes. No product
validation, rendering, preread evidence or publication logic is replaced.
"""

from copy import deepcopy
from dataclasses import replace
import json
import re
import socket
from types import SimpleNamespace

import pytest

from foragent import cli, executors, order
from foragent.executors import ExecResult
from foragent.issue import parse_body

from test_local_truth import record_gh
from test_order import publishing  # noqa: F401
from test_publish_out_of_scope import publish_cli
from test_skills_autosync import cli_exit, isolated_home  # noqa: F401


NUMBER = 1004
COMMANDS = ["true", "test  -s 'safety evidence.txt'"]
CHECKLIST = [
    "Keep the safety proof for the next order.",
    "Keep the operator's review visible.",
]
FILES = ["src/foragent/order.py", "skills/fa-order/**"]
GOAL = "Preserve the safety evidence.\nKeep its context for the next order."
OUT_OF_SCOPE = "Changing the safety policy."
BODY = (
    "<!-- foragent:v1 -->\n"
    f"## Goal\n{GOAL}\n\n"
    f"## Out of scope\n{OUT_OF_SCOPE}\n\n"
    "## Files\n" + "\n".join(f"- {name}" for name in FILES) + "\n\n"
    "## Done when\n" + "".join(f"```sh\n{command}\n```\n" for command in COMMANDS)
    + f"- [x] {CHECKLIST[0]}\n- [ ] {CHECKLIST[1]}\n"
)


@pytest.fixture
def edit_case(publishing, monkeypatch):
    assert parse_body(BODY).done_when_commands == COMMANDS
    assert [item.text for item in parse_body(BODY).done_when_checklist] == CHECKLIST
    root, config, tracker, git, events = publishing
    config = replace(config, language="en", executor="codex", skill_libraries=[])
    monkeypatch.chdir(root)
    main = publish_cli((root, config, tracker, git, events), monkeypatch)
    gh = record_gh(monkeypatch)

    def no_network(*args, **kwargs):
        pytest.fail("These CLI checks must stay offline")

    monkeypatch.setattr(socket.socket, "connect", no_network)

    class Reader:
        def __init__(self):
            self.calls = []

        def run(self, **kwargs):
            self.calls.append(kwargs)
            return ExecResult(0, False, "NONE", kwargs["log_path"])

    reader = Reader()
    for module in (executors, cli, order):
        if hasattr(module, "executor_for"):
            monkeypatch.setattr(module, "executor_for", lambda name: reader)

    tracker.add_issue(NUMBER, "Preserve issue safety", BODY,
                      labels=["lane:api", "size:m", "approved", "custom"])
    value = {
        "id": "edit-keeps-lines-check", "title": "Revise the safety issue",
        "summary": "Keep current acceptance facts while editing work.",
        "spec_markdown": "# Revise the safety issue\n",
        "issues": [],
        "edits": [{
            "number": NUMBER, "goal": GOAL, "out_of_scope": OUT_OF_SCOPE,
            "done_when": list(COMMANDS), "checklist": list(CHECKLIST),
            "files": list(FILES), "acceptance_files": {},
        }],
    }
    return SimpleNamespace(root=root, main=main, tracker=tracker, events=events,
                           reader=reader, gh=gh, plan=value, path=root / "PLAN.json")


def invoke(case, capsys, route="publish", *options):
    case.path.write_text(json.dumps(case.plan), encoding="utf-8")
    argv = ["order", route, str(case.path), "--repo-root", str(case.root)]
    if route == "preread":
        argv.append("--json")
    capsys.readouterr()
    code = cli_exit(case.main, [*argv, *options])
    captured = capsys.readouterr()
    assert case.gh == [], "No GitHub process may run"
    return code, captured.out + captured.err


def set_state(case, state):
    case.tracker.issues[NUMBER] = replace(
        case.tracker.get_issue(NUMBER), state=state,
        state_reason="completed" if state == "closed" else None,
    )


def snapshot(case):
    return deepcopy(case.tracker.issues), {
        number: deepcopy(case.tracker.list_comments(number))
        for number in case.tracker.issues
    }


def assert_refused(case, capsys, route, removed):
    before = snapshot(case)
    options = ("--dry-run",) if route == "dry-run" else ()
    code, message = invoke(case, capsys, "publish" if route == "dry-run" else route, *options)
    assert code != 0, f"{route} silently accepted removed Done when lines: {removed!r}"
    assert str(NUMBER) in message, message
    for line in removed:
        assert line in message, f"{route} must list the exact line {line!r}: {message}"
    assert snapshot(case) == before, "A refusal must preserve issues, labels and comments"
    assert case.events == [], "A refusal must happen before publication effects"
    assert not (case.root / "docs/specs/edit-keeps-lines-check.md").exists()
    assert case.reader.calls == [], "Exact-line validation needs no model judgment"


@pytest.mark.parametrize("state", ["open", "closed"])
@pytest.mark.parametrize("route", ["publish", "dry-run", "preread"])
@pytest.mark.parametrize("loss", ["command", "empty-checklist", "checked-line", "omitted-checklist"])
def test_unexplained_removals_are_refused(edit_case, capsys, state, route, loss):
    """Reject reduced commands, empty/omitted checklists and lost completed text."""
    case = edit_case
    set_state(case, state)
    edit = case.plan["edits"][0]
    if loss == "command":
        edit["done_when"] = COMMANDS[:1]
        removed = COMMANDS[1:]
    elif loss == "checked-line":
        edit["checklist"] = CHECKLIST[1:]
        removed = CHECKLIST[:1]
    else:
        if loss == "omitted-checklist":
            del edit["checklist"]
        else:
            edit["checklist"] = []
        removed = CHECKLIST
    assert_refused(case, capsys, route, removed)


@pytest.mark.parametrize("state", ["open", "closed"])
@pytest.mark.parametrize("kind", ["command", "checklist"])
def test_additions_and_other_edits_then_explained_removal_publish(edit_case, capsys, state, kind):
    """Reject blanket blocking, ignored declarations and reasons kept only in local state."""
    case = edit_case
    set_state(case, state)
    edit = case.plan["edits"][0]
    edit["goal"] += "\nShow the new evidence."
    edit["out_of_scope"] = "Unrelated issue changes."
    edit["files"].append("docs/order.md")
    edit["done_when"].append("test -s added-proof.txt")
    edit["checklist"].append("The added evidence is readable.")
    # An ordinary additive edit needs no removal declarations.
    code, message = invoke(case, capsys)
    assert code == 0, message
    updated = case.tracker.get_issue(NUMBER)
    parsed = parse_body(updated.body)
    assert parsed.goal == edit["goal"] and parsed.out_of_scope == edit["out_of_scope"]
    assert parsed.files == edit["files"]
    assert parsed.done_when_commands == edit["done_when"]
    assert [item.text for item in parsed.done_when_checklist] == edit["checklist"]
    assert updated.state == state and updated.title == "Preserve issue safety"

    # A fresh order removes one exact line and supplies the person's reason.
    case.plan["id"] = "edit-keeps-lines-explained"
    field, line = ("done_when", COMMANDS[1]) if kind == "command" else ("checklist", CHECKLIST[0])
    reason = "A newer acceptance proof covers this obligation; retain this explanation."
    edit[field].remove(line)
    edit["done_when_removals"] = [{"line": line, "reason": reason}]
    code, message = invoke(case, capsys, "preread")
    assert code == 0, message
    code, message = invoke(case, capsys, "publish", "--approve")
    assert code == 0, message
    updated = case.tracker.get_issue(NUMBER)
    parsed = parse_body(updated.body)
    assert parsed.done_when_commands == edit["done_when"]
    assert [item.text for item in parsed.done_when_checklist] == edit["checklist"]
    assert updated.state == state and "approved" in updated.labels
    spec = case.root / "docs/specs/edit-keeps-lines-explained.md"
    readable = re.sub(r"<!--.*?-->", "", updated.body + "\n" + spec.read_text(encoding="utf-8"), flags=re.S)
    assert line in readable and reason in readable, (
        "The removed line and its reason must remain readable on the issue or published spec"
    )


@pytest.mark.parametrize("route", ["publish", "dry-run", "preread"])
@pytest.mark.parametrize("declaration", ["unknown-line", "partial", "empty-reason"])
def test_removal_declarations_must_match_each_current_line(edit_case, capsys, route, declaration):
    """Reject a global opt-out, a stale/nonexistent line and an empty explanation."""
    case = edit_case
    edit = case.plan["edits"][0]
    edit["done_when"] = COMMANDS[:1]
    edit["checklist"] = CHECKLIST[1:]
    edit["done_when_removals"] = [
        {"line": COMMANDS[1], "reason": "Replaced by newer command evidence."},
        {"line": CHECKLIST[0], "reason": "Replaced by newer review evidence."},
    ]
    if declaration == "unknown-line":
        unknown = "test -s a-line-that-is-not-in-this-issue.txt"
        edit["done_when_removals"].append({"line": unknown, "reason": "Stale plan input."})
        offending = [unknown]
    elif declaration == "partial":
        edit["done_when_removals"].pop()
        offending = CHECKLIST[:1]
    else:
        edit["done_when_removals"][0]["reason"] = " \t "
        offending = COMMANDS[1:]
    assert_refused(case, capsys, route, offending)


@pytest.mark.parametrize("state", ["open", "closed"])
def test_order_can_seed_an_edit_from_current_issue_sections(edit_case, capsys, state):
    """Reject requiring an outside helper or seeding without the current Done when."""
    case = edit_case
    set_state(case, state)
    # Read the issue as it is now, rather than a cached order or a fixed template.
    current_command = "test -s evidence-added-after-the-last-order.txt"
    current_checklist = "Keep the later operator's decision visible."
    current_body = BODY + f"```sh\n{current_command}\n```\n- [ ] {current_checklist}\n"
    case.tracker.update_issue(NUMBER, title="Preserve issue safety", body=current_body)
    before = snapshot(case)
    capsys.readouterr()
    code = cli_exit(case.main, ["order", "edit", str(NUMBER), "--repo-root", str(case.root), "--json"])
    captured = capsys.readouterr()
    assert code == 0, f"fa order must provide a supported edit seed: {captured.out}{captured.err}"
    seeded = json.loads(captured.out)
    assert seeded["number"] == NUMBER
    assert seeded["goal"] == GOAL and seeded["out_of_scope"] == OUT_OF_SCOPE
    assert seeded["files"] == FILES
    assert seeded["done_when"] == [*COMMANDS, current_command]
    assert seeded["checklist"] == [*CHECKLIST, current_checklist]
    assert snapshot(case) == before and case.events == [] and case.reader.calls == []
    assert case.gh == []
    # The public output is usable directly as edits[], without hand translation.
    seeded["goal"] += "\nAdd the next piece of evidence."
    case.plan["edits"] = [seeded]
    code, message = invoke(case, capsys, "publish", "--dry-run")
    assert code == 0, message
    code, message = invoke(case, capsys, "preread")
    assert code == 0, message
    code, message = invoke(case, capsys, "publish", "--approve")
    assert code == 0, message
    parsed = parse_body(case.tracker.get_issue(NUMBER).body)
    assert parsed.goal == seeded["goal"]
    assert parsed.done_when_commands == seeded["done_when"]
    assert [item.text for item in parsed.done_when_checklist] == seeded["checklist"]
    assert case.tracker.get_issue(NUMBER).state == state
