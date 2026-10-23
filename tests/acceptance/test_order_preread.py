"""Acceptance test for order-preread. Locked by Foragent; do not edit."""

from dataclasses import replace
import json
from types import SimpleNamespace

import pytest

from test_local_truth import record_gh
from test_orch_review_handoff import make_case
from test_order import plan, publishing
from test_publish_out_of_scope import publish_cli
from test_skills_autosync import cli_exit, isolated_home


UNKNOWN = "Which locale should the client confirmation use?"


@pytest.fixture
def preread_case(publishing, plan, git_history, tmp_path, monkeypatch, request):
    from foragent import cli, executors, order

    executor = getattr(request, "param", "codex")
    root, config, tracker, git, events = publishing
    config = replace(config, executor=executor, executor_model="offline-implementer-model",
                     language="en", skill_libraries=[])
    main = publish_cli((root, config, tracker, git, events), monkeypatch)
    monkeypatch.chdir(root)
    # Reuse the public Executor.run fake with .steps/.calls from make_case.
    runner_case = make_case(git_history, tmp_path / "runner", monkeypatch,
                           executor=executor, skill_libraries=[], test_command="true")
    worker, factories = runner_case.implementer, []

    def factory(name):
        factories.append(name)
        assert name == executor, "Preread must use the configured implementer, not the reviewer"
        return worker

    monkeypatch.setattr(executors, "executor_for", factory)
    # Allow a normal module-level import of the same public factory by the CLI.
    for module in (cli, order):
        if hasattr(module, "executor_for"):
            monkeypatch.setattr(module, "executor_for", factory)
    for index, issue in enumerate(plan["issues"]):
        issue["out_of_scope"] = f"Unrelated {issue['key']} migrations."
        if index == 1:
            path = "tests/acceptance/test_client.py"
            issue["acceptance_files"] = {path: "def test_client_confirmation():\n    assert False, 'client confirmation'\n"}
            issue["done_when"] = [f"uv run pytest -q {path}"]
    path = root / "PLAN.json"
    path.write_text(json.dumps(plan), encoding="utf-8")
    return SimpleNamespace(root=root, plan=plan, path=path, config=config, main=main,
                           tracker=tracker, events=events, worker=worker, factories=factories,
                           gh=record_gh(monkeypatch))


def preread(case, capsys, *responses):
    case.worker.steps = list(responses)
    before = len(case.worker.calls)
    capsys.readouterr()
    code = cli_exit(case.main, ["order", "preread", str(case.path), "--json"])
    captured = capsys.readouterr()
    assert code == 0, f"fa order preread exited {code}: {captured.err.strip()}"
    payload = json.loads(captured.out)
    assert set(payload) == {"issues"} and isinstance(payload["issues"], list)
    assert len(case.worker.calls) - before == len(case.plan["issues"])
    assert case.worker.steps == []
    return payload


def publish(case, capsys, *, blocked_key=None, unknowns=False, dry_run=False):
    before = len(case.worker.calls)
    argv = ["order", "publish", str(case.path), "--approve"]
    if dry_run:
        argv.append("--dry-run")
    capsys.readouterr()
    code = cli_exit(case.main, argv)
    captured = capsys.readouterr()
    message = captured.out + captured.err
    if blocked_key is not None:
        assert code != 0, f"Publish must refuse issue {blocked_key} without a current clean preread"
        assert blocked_key in message, message
        if unknowns:
            assert "unknown" in message.lower() and "resolve" in message.lower(), message
        else:
            assert "preread" in message.lower(), message
            assert "run" in message.lower() or "fa order preread" in message.lower(), message
        assert case.tracker.issues == {} and case.events == []
    else:
        assert code == 0, message
    assert len(case.worker.calls) == before, "Publish must consume preread evidence without invoking a model"
    assert case.gh == []


def assert_published(case):
    children = {issue.title: issue for issue in case.tracker.list_issues()
                if issue.title in {item["title"] for item in case.plan["issues"]}}
    assert len(children) == len(case.plan["issues"])
    assert all("approved" in issue.labels for issue in children.values())
    assert [event[0] for event in case.events] == ["commit", "push", "issue", "issue", "issue"]
    assert case.gh == []


@pytest.mark.parametrize("preread_case", ["codex", "claude"], indirect=True)
def test_order_preread_reads_each_issue_with_implementer_in_read_only_mode(preread_case, capsys):
    case = preread_case
    first, second = "Which region is the login API for?", "Which error should a rejected login display?"
    payload = preread(case, capsys, first + "\n" + second, "NONE")
    assert payload == {"issues": [{"key": "api", "unknowns": [first, second]},
                                 {"key": "client", "unknowns": []}]}
    assert case.factories and set(case.factories) == {case.config.executor}
    for issue, call in zip(case.plan["issues"], case.worker.calls, strict=True):
        prompt = call["prompt"]
        for text in (issue["goal"], issue["out_of_scope"], *issue["done_when"],
                     *issue["acceptance_files"].keys(), *issue["acceptance_files"].values()):
            assert text in prompt, f"Preread for {issue['key']} omitted {text!r}"
        assert "unknown" in prompt.lower() and "NONE" in prompt
        assert "exactly" in prompt.lower(), "The no-unknowns reply must be exactly NONE"
        assert call.get("read_only") is True, "Executor.run must receive read_only=True"
        assert call["model"] == case.config.executor_model
    assert case.tracker.issues == {} and case.events == [] and case.gh == []


def test_order_preread_publish_refuses_plan_without_preread(preread_case, capsys):
    publish(preread_case, capsys, blocked_key="api")


def test_order_preread_latest_unknowns_block_until_a_new_clean_preread(preread_case, capsys):
    case = preread_case
    preread(case, capsys, "NONE", "NONE")
    payload = preread(case, capsys, "NONE", UNKNOWN)
    assert payload == {"issues": [{"key": "api", "unknowns": []},
                                 {"key": "client", "unknowns": [UNKNOWN]}]}
    publish(case, capsys, blocked_key="client", unknowns=True)
    preread(case, capsys, "NONE", "NONE")
    publish(case, capsys)
    assert_published(case)


@pytest.mark.parametrize("changed", ["goal", "out_of_scope", "done_when", "acceptance_files"])
def test_order_preread_plan_content_change_requires_new_preread(preread_case, capsys, changed):
    case = preread_case
    preread(case, capsys, "NONE", "NONE")
    issue = case.plan["issues"][0]
    if changed == "done_when":
        issue[changed].append("test -s feature.txt")
    elif changed == "acceptance_files":
        path = next(iter(issue[changed]))
        issue[changed][path] += "# Also check the selected locale.\n"
    else:
        issue[changed] += " Keep the selected locale."
    # Same filename and order id; only the plan's content has changed.
    case.path.write_text(json.dumps(case.plan), encoding="utf-8")
    publish(case, capsys, blocked_key="api")
    preread(case, capsys, "NONE", "NONE")
    publish(case, capsys)
    assert_published(case)


def test_order_preread_clean_result_follows_content_to_another_plan_path(preread_case, capsys):
    case = preread_case
    preread(case, capsys, "NONE", "NONE")
    copied = case.root / "copied-PLAN.json"
    copied.write_bytes(case.path.read_bytes())
    case.path = copied
    publish(case, capsys)
    assert_published(case)


def test_order_preread_dry_run_allows_missing_unknown_and_stale_results(preread_case, capsys):
    case = preread_case
    for state in ("missing", "unknown", "stale"):
        if state == "unknown":
            preread(case, capsys, "NONE", UNKNOWN)
        elif state == "stale":
            preread(case, capsys, "NONE", "NONE")
            case.plan["issues"][0]["goal"] += " Keep the locale."
            case.path.write_text(json.dumps(case.plan), encoding="utf-8")
        before = {str(path.relative_to(case.root)): path.read_bytes()
                  for path in case.root.rglob("*") if path.is_file()}
        publish(case, capsys, dry_run=True)
        assert case.tracker.issues == {} and case.events == []
        assert {str(path.relative_to(case.root)): path.read_bytes()
                for path in case.root.rglob("*") if path.is_file()} == before
