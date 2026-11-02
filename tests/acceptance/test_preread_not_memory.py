"""Locked: preread survives lane learning, while changed work needs a new read.

Exercise fa order preread/publish/flush and fa gate run-issue. Only the existing
offline Git, tracker and executor adapters are faked; evidence and prompts are real.
"""

from copy import deepcopy
from dataclasses import replace
import hashlib
import json
import socket

import pytest

from conftest import ACCEPTANCE, git_command
from foragent import executors
from test_order import plan, publishing  # noqa: F401
from test_order_preread import preread, preread_case  # noqa: F401
from test_orch_review_handoff import bind_cli, make_case
from test_publish_out_of_scope import publish_cli
from test_skills_autosync import cli_exit, isolated_home  # noqa: F401


BOUNDARY = "after-lane-learning"
MEMORY = ".claude/agent-memory/fa-api"
AGENT = ".claude/agents/fa-api.md"
APPROACH = "Keep the confirmation contract and reuse the existing writer."
CHOICE = "Choose a reversible helper name during implementation."
REPLY = f"APPROACH: {APPROACH}\nDECIDE: {CHOICE}"
OLD = "A lesson known before preread."
CURRENT = "A lesson learned after preread."


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Use offline service adapters, never a real model or network")

    for adapter in (executors.CodexExecutor, executors.ClaudeExecutor):
        monkeypatch.setattr(adapter, "run", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)


@pytest.fixture
def memory_case(preread_case):
    case = preread_case
    agent = case.root / AGENT
    agent.parent.mkdir(parents=True, exist_ok=True)
    agent.write_text("---\nname: fa-api\n---\nKeep the confirmation contract.\n")
    folder = case.root / MEMORY
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "MEMORY.md").write_text(OLD + "\n")
    (folder / "MEMORY.new.md").write_text("- [id:before] A pending lane lesson.\n")
    return case


def invoke(case, capsys, *args):
    capsys.readouterr()
    code = cli_exit(case.main, ["order", *map(str, args), "--repo-root", str(case.root)])
    output = capsys.readouterr()
    assert case.gh == [], "GitHub transport must stay fake"
    return code, output.out + output.err


def learn(case, change):
    folder = case.root / MEMORY
    if change == "curated":
        (folder / "MEMORY.md").write_text(CURRENT + "\n")
    elif change == "pending":
        with (folder / "MEMORY.new.md").open("a") as output:
            output.write(f"- [id:after] {CURRENT}\n")
    elif change == "consumed":
        (folder / "MEMORY.consumed.ids").write_text("before\n")
    elif change == "created":
        (folder / "MEMORY.md").write_text(CURRENT + "\n")
        (folder / "MEMORY.new.md").write_text(f"- [id:after] {CURRENT}\n")
    else:
        assert change == "native-cut"
        with (folder / "MEMORY.md").open("a") as output:
            output.write(CURRENT + "\n")


@pytest.mark.parametrize("preread_case", ["codex", "claude"], indirect=True)
@pytest.mark.parametrize("route", ["publish", "flush-four"])
@pytest.mark.parametrize("change", ["curated", "pending", "consumed", "created", "native-cut"])
def test_memory_only_change_keeps_clean_prereads(memory_case, capsys, route, change):
    case = memory_case
    if change == "created":
        for name in ("MEMORY.md", "MEMORY.new.md"):
            (case.root / MEMORY / name).unlink()
    elif change == "native-cut":
        (case.root / AGENT).write_text(
            "---\nname: fa-api\nmemory: project\n---\nKeep the confirmation contract.\n")
        (case.root / MEMORY / "MEMORY.md").write_text("A known lane lesson.\n" * 201)
    template, orders = deepcopy(case.plan), []
    for index in range(4 if route == "flush-four" else 1):
        case.plan = deepcopy(template)
        case.plan["id"] += f"-memory-{index}"
        case.plan["title"] += f" {index}"
        for item in case.plan["issues"]:
            item["title"] += f" {index}"
        case.path = case.root / f"order-{index}.json"
        case.path.write_text(json.dumps(case.plan))
        preread(case, capsys, REPLY, "NONE")
        orders.append(deepcopy(case.plan))
        if route == "flush-four":
            code, message = invoke(case, capsys, "publish", case.path, "--approve", "--defer", BOUNDARY)
            assert code == 0, message
    assert case.events == [] and case.tracker.list_issues() == []
    learn(case, change)
    before = len(case.worker.calls)
    args = ("flush", "--boundary", BOUNDARY) if route == "flush-four" else (
        "publish", case.path, "--approve")
    code, message = invoke(case, capsys, *args)
    assert code == 0, f"Lane learning must preserve a clean preread: {message}"
    assert len(case.worker.calls) == before, "Publication must reuse evidence without another model call"
    issues = case.tracker.list_issues()
    assert len(issues) == 3 * len(orders)
    for value in orders:
        for item in value["issues"]:
            child, = [issue for issue in issues if issue.title == item["title"]]
            assert "approved" in child.labels
            for path, content in item["acceptance_files"].items():
                assert (case.root / path).read_text() == content
    assert [event[0] for event in case.events].count("commit") == 1
    assert [event[0] for event in case.events].count("push") == 1
    if route == "flush-four":
        events = list(case.events)
        code, message = invoke(case, capsys, *args)
        assert code == 0, message
        assert case.events == events, "A completed flush must not repeat publication"


@pytest.mark.parametrize("preread_case", ["codex", "claude"], indirect=True)
@pytest.mark.parametrize("route,change", [
    (route, change) for route in ("publish", "defer")
    for change in ("issue", "locked-tests", "executor", "model", "lane-instructions")
] + [("flush", change) for change in ("executor", "model", "lane-instructions")])
def test_changed_work_still_refuses_with_the_existing_message(
    memory_case, publishing, monkeypatch, capsys, route, change,
):
    case = memory_case
    preread(case, capsys, REPLY, "NONE")
    if route == "flush":
        code, message = invoke(case, capsys, "publish", case.path, "--approve", "--defer", BOUNDARY)
        assert code == 0, message
    learn(case, "pending")
    item = case.plan["issues"][0]
    if change == "issue":
        item["goal"] += " Preserve the selected locale too."
    elif change == "locked-tests":
        path = next(iter(item["acceptance_files"]))
        item["acceptance_files"][path] += "# The locked contract changed.\n"
    elif change == "executor":
        case.config = replace(case.config, executor="claude" if case.config.executor == "codex" else "codex")
    elif change == "model":
        case.config = replace(case.config, executor_model="another-implementer-model")
    else:
        with (case.root / AGENT).open("a") as output:
            output.write("Preserve the new lane instruction too.\n")
    case.path.write_text(json.dumps(case.plan))
    case.main = publish_cli((case.root, case.config, case.tracker, publishing[3], case.events), monkeypatch)
    before = len(case.worker.calls)
    args = ("flush", "--boundary", BOUNDARY) if route == "flush" else (
        "publish", case.path, "--approve", *(("--defer", BOUNDARY) if route == "defer" else ()))
    code, message = invoke(case, capsys, *args)
    assert code != 0, f"Changed {change} requires another preread"
    assert "no preread of the current content for issue(s)" in message, message
    assert "api" in message and "run fa order preread PLAN.json first" in message, message
    assert len(case.worker.calls) == before
    assert case.events == [] and case.tracker.list_issues() == []


@pytest.mark.parametrize("executor", ["codex", "claude"])
def test_latest_earlier_plan_and_current_memory_reach_the_implementing_run(
    git_history, tmp_path, monkeypatch, capsys, executor,
):
    git_history.update({
        ".foragent.yml": "repo: owner/repo\n",
        AGENT: "---\nname: fa-api\n---\nKeep the confirmation contract.\n",
        f"{MEMORY}/MEMORY.md": OLD + "\n",
        f"{MEMORY}/MEMORY.new.md": "- [id:before] A pending lane lesson.\n",
    })
    git_command(git_history.clone, "fetch", "origin", "main")
    git_command(git_history.clone, "merge", "--ff-only", "origin/main")
    case = make_case(git_history, tmp_path, monkeypatch, executor=executor,
                     reviewer="claude" if executor == "codex" else "codex", language="en",
                     skill_libraries=[], test_command="true",
                     lanes={"api": {"paths": ["feature.txt"], "agent": "fa-api", "skills": []}})
    main = bind_cli(case, monkeypatch)
    for name, adapter in (("codex", executors.CodexExecutor), ("claude", executors.ClaudeExecutor)):
        worker = case.implementer if name == executor else case.reviewer
        monkeypatch.setattr(adapter, "run", lambda self, _worker=worker, **kw: _worker.run(**kw))
    value = {"id": "memory-handoff", "title": "Keep confirmation", "summary": "Keep the confirmation contract.",
             "spec_markdown": "# Confirmation\nKeep the confirmation contract.\n", "issues": [{
                 "key": "feature", "title": "Write the confirmation", "lane": "api", "size": "m",
                 "goal": "Implement the feature.", "out_of_scope": "Other features.",
                 "done_when": ["test -f feature.txt", f"test -f {ACCEPTANCE}"],
                 "files": ["feature.txt"], "acceptance_files": {}}]}
    path = case.root / "handoff.json"
    path.write_text(json.dumps(value))
    case.implementer.steps = ["APPROACH: Obsolete proposal.\nDECIDE: Obsolete choice.", REPLY]
    for _ in range(2):
        assert cli_exit(main, ["order", "preread", str(path), "--json"]) == 0
        assert json.loads(capsys.readouterr().out)["issues"][0]["unknowns"] == []
    git_history.update({f"{MEMORY}/MEMORY.md": CURRENT + "\n",
                        f"{MEMORY}/MEMORY.new.md": "- [id:after] A fresh pending lesson.\n"})
    case.git.update_base("main")  # A merge whose only changed files are lane memory.
    with monkeypatch.context() as publisher:
        publisher.setattr(case.tracker, "strict", False)
        publisher.setattr(case.tracker, "actor", "human")
        code = cli_exit(main, ["order", "publish", str(path), "--approve"])
        output = capsys.readouterr()
        assert code == 0, output.out + output.err
    child, = [issue for issue in case.tracker.list_issues() if issue.title == "Write the confirmation"]
    body = child.body
    # Seed the normal scheduler-owned approval/run state using its existing public store API.
    case.store.save_approval(child.number, body=body,
                             body_sha=hashlib.sha256(body.strip().encode()).hexdigest(),
                             acceptance_blobs=case.git.blob_shas("origin/main", [ACCEPTANCE]),
                             approved_by="human", approved_at=case.clock[0].isoformat())
    case.store.start_run(child.number, branch=f"foragent/issue-{child.number}",
                         worktree=case.store.worktree_path(child.number), lane="api")
    case.implementer.calls.clear()
    case.implementer.steps = [case.implement]
    case.reviewer.steps = [json.dumps({"result": "(none)"}) if executor == "codex" else "VERDICT: PASS"]
    code = cli_exit(main, ["gate", "run-issue", str(child.number), "--repo-root", str(case.root)])
    output = capsys.readouterr()
    assert code == 0 and case.store.get_run(child.number).outcome == "merged", output.out + output.err
    call, = case.implementer.calls
    prompt = call["prompt"]
    for text in ("Your lane's earlier plan", APPROACH, CHOICE, CURRENT, "A fresh pending lesson."):
        assert prompt.count(text) == 1, f"The implementing run must receive {text!r} once"
    for text in ("Obsolete proposal.", "Obsolete choice.", OLD, "A pending lane lesson."):
        assert text not in prompt, f"The implementing run received stale context: {text!r}"
