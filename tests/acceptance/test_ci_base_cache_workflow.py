"""Locked acceptance rejecting PR-scoped cache publication and missing trusted production."""

import ast
from fnmatch import fnmatchcase
import json
from pathlib import Path
import re

import pytest
import yaml

from foragent import cli
from foragent.ci_workflow import WORKFLOW
from test_local_truth import record_gh
from test_skills_autosync import cli_exit


ROOT = next(parent for parent in Path(__file__).resolve().parents
            if (parent / ".foragent.yml").is_file() and (parent / "src/foragent/ci_shard.py").is_file())
BASE_SHA, HEAD_SHA, MERGE_SHA = "a" * 40, "b" * 40, "c" * 40
UNKNOWN = object()
EXPRESSION = re.compile(r"\$\{\{(.*?)\}\}", re.DOTALL)


def condition(value, context):
    # Evaluate event/ref facts, keeping runtime outputs unknown. An unknown
    # condition is potentially executable: it cannot conceal a PR cache save.
    # This is expression parsing in a test, not a production decision rule.
    if not isinstance(value, str):
        return value
    source = value.strip().removeprefix("${{").removesuffix("}}").strip()
    source = re.sub(r"!(?!=)", "not ", source.replace("&&", " and ").replace("||", " or ")).strip()

    def visit(node):
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name):
            return context.get(node.id, {"true": True, "false": False, "null": None}.get(node.id, UNKNOWN))
        if isinstance(node, ast.Attribute):
            parent = visit(node.value)
            return parent.get(node.attr, UNKNOWN) if isinstance(parent, dict) else UNKNOWN
        if isinstance(node, ast.Subscript):
            parent, key = visit(node.value), visit(node.slice)
            return parent.get(key, UNKNOWN) if isinstance(parent, dict) and key is not UNKNOWN else UNKNOWN
        if isinstance(node, ast.BoolOp):
            values = [visit(child) for child in node.values]
            known = [bool(item) for item in values if item is not UNKNOWN]
            if isinstance(node.op, ast.And):
                return False if False in known else UNKNOWN if UNKNOWN in values else all(known)
            return True if True in known else UNKNOWN if UNKNOWN in values else any(known)
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            operand = visit(node.operand)
            return UNKNOWN if operand is UNKNOWN else not operand
        if isinstance(node, ast.Compare) and len(node.ops) == 1:
            left, right = visit(node.left), visit(node.comparators[0])
            if left is UNKNOWN or right is UNKNOWN:
                return UNKNOWN
            if isinstance(node.ops[0], ast.Eq):
                return left == right
            if isinstance(node.ops[0], ast.NotEq):
                return left != right
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            args = [visit(arg) for arg in node.args]
            functions = {"startsWith": str.startswith, "endsWith": str.endswith,
                         "contains": lambda container, item: item in container,
                         "format": lambda template, *values: template.format(*values),
                         "success": lambda: True, "always": lambda: True,
                         "failure": lambda: False, "cancelled": lambda: False}
            if node.func.id in functions and all(arg is not UNKNOWN for arg in args):
                return functions[node.func.id](*args)
        return UNKNOWN

    return visit(ast.parse(source, mode="eval").body)


def render(value, context):
    return EXPRESSION.sub(lambda match: match.group(0) if (result := condition(match.group(1), context)) is UNKNOWN
                          else str(result), str(value))


def event_context(event, branch, *, number=101, shard=0):
    context = {"github": {"event_name": event, "repository": "owner/repo",
                       "sha": BASE_SHA if event == "push" else MERGE_SHA,
                       "ref": f"refs/heads/{branch}" if event == "push" else f"refs/pull/{number}/merge",
                       "ref_name": branch if event == "push" else f"{number}/merge",
                       "event": {"repository": {"default_branch": "main"},
                                 "pull_request": {"number": number,
                                                  "base": {"sha": BASE_SHA, "ref": branch},
                                                  "head": {"sha": HEAD_SHA if number == 101 else "d" * 40,
                                                           "ref": f"foragent/issue-{number}",
                                                           "repo": {"full_name": "owner/repo"}}}}},
            "matrix": {"shard": shard}, "runner": {"temp": "/runner/temp"}}
    if event == "push":
        context["github"]["event"].pop("pull_request")
    return context


def active_steps(document, context):
    github = context["github"]
    triggers = document.get("on", document.get(True))
    triggers = {name: None for name in ([triggers] if isinstance(triggers, str) else triggers)} if not isinstance(triggers, dict) else triggers
    if github["event_name"] not in triggers:
        return []
    options = triggers[github["event_name"]] or {}
    branch = github["ref_name"] if github["event_name"] == "push" else github["event"]["pull_request"]["base"]["ref"]
    if "branches" in options and not any(fnmatchcase(branch, pattern) for pattern in options["branches"]):
        return []
    if any(fnmatchcase(branch, pattern) for pattern in options.get("branches-ignore", [])):
        return []
    result = []
    for job in document["jobs"].values():
        shards = job.get("strategy", {}).get("matrix", {}).get("shard")
        if (shards is not None and context["matrix"]["shard"] not in shards) or condition(job.get("if", True), context) is False:
            continue
        for index, step in enumerate(job.get("steps", [])):
            env = {**document.get("env", {}), **job.get("env", {}), **step.get("env", {})}
            for _ in range(len(env) + 1):
                expanded = {key: render(value, {**context, "env": env}) for key, value in env.items()}
                if expanded == env:
                    break
                env = expanded
            local = {**context, "env": env}
            if condition(step.get("if", True), local) is not False:
                result.append({"job": job, "step": step, "index": index, "env": env,
                               "inputs": {key: render(value, local) for key, value in step.get("with", {}).items()}})
    return result


def caches(rows, *, save):
    actions = {"actions/cache", "actions/cache/save"} if save else {"actions/cache", "actions/cache/restore"}
    return [row for row in rows if row["step"].get("uses", "").split("@", 1)[0] in actions]


def measurement(rows, *, producer):
    values = [row for row in rows if "foragent.ci_shard" in row["step"].get("run", "")
              and (row["env"].get("FA_BASE_CACHE_MODE") == "produce") is producer]
    assert len(values) == 1, "Each active shard needs one trusted producer or PR comparison entry"
    return values[0]


@pytest.fixture
def generate_workflow(tmp_path, monkeypatch):
    record_gh(monkeypatch, lambda argv: json.dumps({"isPrivate": False}))

    def generate(branch=None):
        root = tmp_path / "project"
        root.mkdir(exist_ok=True)
        config = yaml.safe_load((ROOT / ".foragent.yml").read_text())
        if branch is not None:
            config["base_branch"] = branch
        (root / ".foragent.yml").write_text(yaml.safe_dump(config), encoding="utf-8")
        assert cli_exit(cli.main, ["ci", "workflow", "--repo-root", str(root)]) == 0
        text = (root / WORKFLOW).read_text(encoding="utf-8")
        return yaml.safe_load(text), text

    return generate


@pytest.mark.parametrize("branch", ["main", "release/train"])
def test_base_push_produces_and_saves_the_full_node_pool_using_the_pushed_commit(generate_workflow, branch):
    """Reject PR-only warmup, PR-SHA writer keys and full records tied to the PR shard layout."""
    document, _ = generate_workflow(branch)
    rows = active_steps(document, event_context("push", branch))
    producer = measurement(rows, producer=True)
    snapshot = producer["env"].get("FA_BASE_CACHE_FILE")
    assert snapshot, "Base-branch push must produce the full unexcluded node-result pool"
    assert producer["env"].get("FA_BASE_SHA") == BASE_SHA
    assert producer["env"].get("FA_HEAD_SHA", BASE_SHA) == BASE_SHA
    saves = [row for row in caches(rows, save=True) if snapshot in row["inputs"].get("path", "").splitlines()]
    assert saves, "Base-branch production must publish reusable per-node coverage"
    for save in saves:
        key = save["inputs"].get("key", "")
        assert BASE_SHA in key and "pull_request" not in key
        if save["step"]["uses"].startswith("actions/cache/save@"):
            assert save["job"] is producer["job"] and save["index"] > producer["index"]
    unrelated = active_steps(document, event_context("push", "unrelated"))
    assert not caches(unrelated, save=True), "An unrelated branch push cannot publish trusted base records"


@pytest.mark.parametrize("branch", ["main", "release/train"])
def test_prs_restore_the_shared_base_key_without_any_cache_save(generate_workflow, branch):
    """Reject PR publication, shard-only lookup and PR keys that never reach the full base-push record."""
    document, _ = generate_workflow(branch)
    pushed = active_steps(document, event_context("push", branch))
    saved_keys = [row["inputs"].get("key", "") for row in caches(pushed, save=True)]
    lookups = []
    for shard in range(4):
        for number in (101, 202):
            rows = active_steps(document, event_context("pull_request", branch, number=number, shard=shard))
            compare = measurement(rows, producer=False)
            snapshot = compare["env"].get("FA_BASE_CACHE_FILE")
            assert snapshot and snapshot != compare["env"].get("FA_REPORT_FILE")
            # A combined actions/cache action has a post-save, even if its
            # visible step appears before comparison. PRs must use restore-only.
            assert not [row for row in caches(rows, save=True)
                        if snapshot in row["inputs"].get("path", "").splitlines()], (
                "No PR-reachable base-record save, including implicit post-save"
            )
            restores = [row for row in caches(rows, save=False)
                        if snapshot in row["inputs"].get("path", "").splitlines()]
            assert restores, "PR shards must restore the trusted base-branch record"
            for restore in restores:
                assert restore["job"] is compare["job"] and restore["index"] < compare["index"]
                prefixes = [restore["inputs"].get("key", ""),
                            *restore["inputs"].get("restore-keys", "").splitlines()]
                prefixes = [key for key in prefixes if key and BASE_SHA in key]
                assert prefixes and all(HEAD_SHA not in key and MERGE_SHA not in key for key in prefixes)
                # Runtime selection hashes may use different step IDs on push
                # and PR. Normalize those opaque expressions, not the base commit.
                normalized = lambda key: EXPRESSION.sub("<selection>", key)
                assert any(normalized(saved).startswith(normalized(prefix))
                           for saved in saved_keys for prefix in prefixes), "PR lookup must match a base-push save key"
                lookups.append(prefixes)
            assert any(row["step"].get("uses", "").startswith("actions/upload-artifact@")
                       and row["inputs"].get("name") == f"acceptance-{shard}" for row in rows)
    assert lookups and all(value == lookups[0] for value in lookups), (
        "All PR heads and shard assignments must reach the same full per-node coverage"
    )


def test_committed_workflow_equals_cli_output_with_trusted_base_production(generate_workflow):
    """Reject stale committed YAML and a drift-only test that passes without a trusted push producer."""
    document, generated = generate_workflow()
    assert (ROOT / WORKFLOW).read_text(encoding="utf-8") == generated
    rows = active_steps(document, event_context("push", "main"))
    producer = measurement(rows, producer=True)
    assert producer["env"].get("FA_BASE_CACHE_FILE") and caches(rows, save=True)
