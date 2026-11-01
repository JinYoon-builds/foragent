"""Locked acceptance for trusted per-node base reuse across changing PR exclusions.

FA_BASE_CACHE_FILE is the restored full-selection result pool. A trusted push
mints it through main(FA_BASE_CACHE_MODE=produce), without open-issue exclusions.
The public JSON envelope has results keyed by node ID and scope containing
acceptance_dir, command and marker. Individual values and provenance metadata
stay opaque. Removing one entry models partial
availability from the trusted cache transport, without inventing test results.
PRs read only, measure only uncovered selected base nodes, and keep today's
comparison report format and exclusions. Real pytest bodies log executed IDs.
"""

from contextlib import nullcontext
import builtins
import io
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest
import yaml

from conftest import ACCEPTANCE, git_command
from foragent import ci_shard
from foragent.ci_mode import comparison
from foragent.ci_transport import decode_report
from foragent.ci_workflow import EXCLUSIONS_TRAILER
from test_ci_mode_unit import report


KEEP_FILE = "tests/acceptance/test_cache_keep.py"
KEEP = KEEP_FILE + "::test_keep"
ALREADY_FAILED = KEEP_FILE + "::test_already_failed"
OPTIONAL_FILE = "tests/acceptance/test_cache_extra.py"
OPTIONAL = OPTIONAL_FILE + "::test_optional"
CROSS_FILE = "tests/acceptance/test_cache_optional.py"
CROSS = CROSS_FILE + "::test_cross"
FEATURE = ACCEPTANCE + "::test_feature"
ALL_NODES = {KEEP, ALREADY_FAILED, OPTIONAL, CROSS, FEATURE}


@pytest.fixture
def shard_case(git_history, tmp_path, monkeypatch):
    root = git_history.clone
    execution = tmp_path / "executed.jsonl"
    config = {"checks": "ci", "ci_shards": 2,
              "test_command": f"{sys.executable} -m pytest"}
    case = SimpleNamespace(root=root, history=git_history,
                           cache=tmp_path / "restored-base.json", serial=0)

    def source(path, functions):
        text = "from pathlib import Path\nimport json\n"
        for name, passing in functions:
            text += (f"def {name}():\n"
                     f"    with Path({str(execution)!r}).open('a') as stream:\n"
                     f"        stream.write(json.dumps({{'cwd': str(Path.cwd()), 'nodeid': {path + '::' + name!r}}}) + '\\n')\n"
                     f"    assert {passing!r}\n")
        return text

    files = {".foragent.yml": yaml.safe_dump(config),
             KEEP_FILE: source(KEEP_FILE, [("test_keep", True), ("test_already_failed", False)]),
             OPTIONAL_FILE: source(OPTIONAL_FILE, [("test_optional", True)]),
             CROSS_FILE: source(CROSS_FILE, [("test_cross", True)]),
             ACCEPTANCE: source(ACCEPTANCE, [("test_feature", True)])}
    # The pushed commit itself carries an old open-issue trailer. Production
    # must still cover OPTIONAL, and every other node, across both PR shards.
    git_history.update(files, message=f"Base\n\n{EXCLUSIONS_TRAILER}: {json.dumps([OPTIONAL_FILE])}")
    case.base = git_history.tip
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")

    def observed():
        rows = [json.loads(line) for line in execution.read_text().splitlines()] if execution.exists() else []
        case.base_ids = [row["nodeid"] for row in rows if Path(row["cwd"]) != root]
        case.head_ids = [row["nodeid"] for row in rows if Path(row["cwd"]) == root]

    def publish(files=None, *, excluded=()):
        case.excluded = list(excluded)
        git_history.update(files or {"head-note.txt": str(case.serial)},
                           message=f"Offline head\n\n{EXCLUSIONS_TRAILER}: {json.dumps(sorted(excluded))}")
        git_command(root, "fetch", "-q", "origin")  # Local bare fixture only.
        git_command(root, "merge", "-q", "--ff-only", "origin/main")
        case.head = git_history.tip

    def produce(*, event="push", source_ref="refs/heads/main"):
        case.serial += 1
        producer_root = tmp_path / f"base-push-{case.serial}"
        case.cache.unlink(missing_ok=True)
        git_command(root, "worktree", "add", "--detach", str(producer_root), case.base)
        event_file = tmp_path / f"push-event-{case.serial}.json"
        event_file.write_text(json.dumps({"ref": source_ref, "after": case.base,
                                         "repository": {"full_name": "owner/repo", "default_branch": "main"}}))
        try:
            with monkeypatch.context() as patch:
                settings = {"FA_CHECKOUT": str(producer_root), "FA_REPOSITORY": "owner/repo",
                            "FA_BASE_SHA": case.base, "FA_HEAD_SHA": case.base,
                            # Producer coverage is the full portable selection,
                            # independent of the PR's two-shard layout.
                            "FA_SHARD": "0", "FA_SHARDS": "1",
                            "FA_BASE_CACHE_FILE": str(case.cache), "FA_BASE_CACHE_MODE": "produce",
                            "GITHUB_EVENT_NAME": event, "GITHUB_REF": source_ref,
                            "GITHUB_SHA": case.base, "GITHUB_RUN_ID": str(case.serial),
                            "GITHUB_EVENT_PATH": str(event_file),
                            "GITHUB_OUTPUT": str(tmp_path / f"push-outputs-{case.serial}")}
                for key, value in settings.items():
                    patch.setenv(key, value)
                patch.delenv("FA_REPORT_FILE", raising=False)
                execution.write_text("")
                if event == "push":
                    ci_shard.main()
                    assert_recorded(case)
                    observed()
                    assert set(case.base_ids) == ALL_NODES and len(case.base_ids) == len(ALL_NODES)
                    assert case.head_ids == []
                else:
                    try:
                        ci_shard.main()
                    except ValueError:
                        pass
                    assert not case.cache.exists(), "A PR cannot mint a trusted reusable base record"
        finally:
            git_command(root, "worktree", "remove", "--force", str(producer_root))

    def invoke(*, shard=0, cache=True, number=101):
        case.serial += 1
        artifact = tmp_path / f"report-{case.serial}.json"
        output = tmp_path / f"outputs-{case.serial}"
        event_file = tmp_path / f"pr-event-{case.serial}.json"
        event_file.write_text(json.dumps({"number": number, "repository": {"full_name": "owner/repo", "default_branch": "main"},
                                         "pull_request": {"base": {"sha": case.base, "ref": "main"},
                                                          "head": {"sha": case.head, "ref": f"foragent/issue-{number}",
                                                                   "repo": {"full_name": "owner/repo"}}}}))
        settings = {"GITHUB_EVENT_NAME": "pull_request", "GITHUB_REF": f"refs/pull/{number}/merge",
                    "GITHUB_SHA": "f" * 40, "GITHUB_RUN_ID": str(1000 + case.serial),
                    "GITHUB_EVENT_PATH": str(event_file),
                    "FA_CHECKOUT": str(root), "FA_REPOSITORY": "owner/repo",
                    "FA_BASE_SHA": case.base, "FA_HEAD_SHA": case.head,
                    "FA_SHARD": str(shard), "FA_SHARDS": "2",
                    "FA_REPORT_FILE": str(artifact), "GITHUB_OUTPUT": str(output)}
        for key, value in settings.items():
            monkeypatch.setenv(key, value)
        monkeypatch.delenv("FA_BASE_CACHE_MODE", raising=False)
        if cache:
            monkeypatch.setenv("FA_BASE_CACHE_FILE", str(case.cache))
        else:
            monkeypatch.delenv("FA_BASE_CACHE_FILE", raising=False)
        execution.write_text("")
        ci_shard.main()
        observed()
        payload = decode_report(artifact.read_text(encoding="utf-8"))
        assert output.read_text(encoding="utf-8") == "comparison=true\n"
        assert payload["base_sha"] == case.base and payload["head_sha"] == case.head
        assert payload["shard_index"] == shard and payload["shard_count"] == 2
        assert payload["base"]["complete"] is True and payload["head"]["complete"] is True
        return payload

    def break_node(node):
        if node == KEEP:
            return {KEEP_FILE: source(KEEP_FILE, [("test_keep", False), ("test_already_failed", False)])}
        return {OPTIONAL_FILE: source(OPTIONAL_FILE, [("test_optional", False)])}

    case.publish, case.invoke, case.produce, case.break_node = publish, invoke, produce, break_node
    publish()
    return case


def assert_recorded(case):
    assert case.cache.is_file() and case.cache.stat().st_size, (
        "A trusted base-branch push must produce FA_BASE_CACHE_FILE for later PRs"
    )
    value = json.loads(case.cache.read_text())
    assert isinstance(value.get("results"), dict) and ALL_NODES <= value["results"].keys(), (
        "The trusted record must cover every portable node without open-issue exclusions"
    )
    assert isinstance(value.get("scope"), dict) and {"acceptance_dir", "command", "marker"} <= value["scope"].keys()


def verdict(payload):
    evidence = report(index=payload["shard_index"], count=2)
    evidence["report"] = payload
    evidence["run"]["head_sha"] = evidence["artifact"]["workflow_run"]["head_sha"] = payload["head_sha"]
    # These cases compare one shard at a time. The existing envelope fake
    # supplies the other shard as complete and empty, without changing policy.
    other = report(index=1 - payload["shard_index"], count=2)
    other["run"]["head_sha"] = other["artifact"]["workflow_run"]["head_sha"] = payload["head_sha"]
    other["report"].update(base_sha=payload["base_sha"], head_sha=payload["head_sha"],
                           excluded=payload["excluded"], scope=payload["scope"])
    for side in ("base", "head"):
        other["report"][side] = {"complete": True, "collected_ids": [], "passing_ids": []}
    return comparison([evidence, other], "owner/repo", payload["base_sha"], payload["head_sha"],
                      payload["excluded"], expected_scope=payload["scope"], expected_shards=range(2))[0]


def same_verdict(cached, full):
    assert cached["base"] == full["base"] and cached["head"] == full["head"]
    assert verdict(cached) == verdict(full)


@pytest.mark.parametrize("head_result", ["passing", "failed", "deselected"])
def test_hit_skips_base_across_pr_heads_and_keeps_the_full_run_verdict(shard_case, head_result):
    """Reject PR-written hit fixtures, repeated covered base execution and waived failed/deselected regressions."""
    case = shard_case
    case.produce()
    trusted = case.cache.read_bytes()
    first = case.invoke()
    assert case.base_ids == [] and KEEP in first["base"]["passing_ids"]
    assert ALREADY_FAILED not in first["base"]["passing_ids"]
    if head_result == "failed":
        case.publish(case.break_node(KEEP))
    elif head_result == "deselected":
        # Rename just the head's function. The original base node must remain
        # protected even though head collection no longer contains it.
        content = case.break_node(KEEP)[KEEP_FILE].replace("def test_keep():", "def helper_keep():")
        case.publish({KEEP_FILE: content})
    else:
        case.publish({"another-pr.txt": "same base, different head\n"})
    cached = case.invoke(number=202)
    assert case.base_ids == [] and case.head_ids
    assert case.cache.read_bytes() == trusted
    full = case.invoke(cache=False)
    assert KEEP in case.base_ids
    same_verdict(cached, full)
    assert verdict(cached) == ([] if head_result == "passing" else [KEEP])


@pytest.mark.parametrize("exclusions", [[OPTIONAL_FILE], [], [KEEP_FILE]], ids=["added", "removed", "replaced"])
@pytest.mark.parametrize("regressed", [False, True], ids=["passing", "regression"])
def test_exclusion_changes_reuse_covered_nodes_with_the_uncached_verdict(shard_case, exclusions, regressed):
    """Reject exclusion-keyed records, forgetting newly unlocked coverage and excluding a genuine regression."""
    case = shard_case
    case.produce()
    trusted = case.cache.read_bytes()
    target = OPTIONAL if KEEP_FILE in exclusions else KEEP
    case.publish(case.break_node(target) if regressed else None, excluded=exclusions)
    cached = case.invoke()
    assert case.base_ids == [], "Changing open-issue exclusions must not remeasure covered nodes"
    assert case.cache.read_bytes() == trusted
    full = case.invoke(cache=False)
    assert target in case.base_ids
    same_verdict(cached, full)
    assert verdict(cached) == ([target] if regressed else [])


@pytest.mark.parametrize("missing", [KEEP, OPTIONAL], ids=["one-node-in-shared-file", "one-file-node"])
@pytest.mark.parametrize("regressed", [False, True], ids=["passing", "regression"])
def test_partial_miss_runs_only_the_unrecorded_selected_base_node(shard_case, missing, regressed):
    """Reject whole-suite/file fallback, treating missing nodes as failures and silently dropping uncovered protection."""
    case = shard_case
    case.produce()
    # Trusted cache retrieval can supply an incomplete node pool. Preserve the
    # genuine producer metadata and all other genuine per-node values.
    restored = json.loads(case.cache.read_text())
    restored["results"].pop(missing)
    case.cache.write_text(json.dumps(restored))
    partial = case.cache.read_bytes()
    if regressed:
        case.publish(case.break_node(missing))
    cached = case.invoke()
    assert case.base_ids == [missing], "Only the missing selected node may execute on base"
    assert case.cache.read_bytes() == partial, "A PR must not publish its partial-miss repair"
    full = case.invoke(cache=False)
    assert len(case.base_ids) > 1 and missing in case.base_ids
    same_verdict(cached, full)
    assert verdict(cached) == ([missing] if regressed else [])


@pytest.mark.parametrize("regressed", [False, True], ids=["excluded-only-failure", "also-selected-failure"])
def test_excluded_recorded_results_never_affect_the_comparison(shard_case, regressed):
    """Reject leaking excluded cached passes into regression checks or waiving selected failures with exclusions."""
    case = shard_case
    case.produce()
    changes = case.break_node(KEEP)
    if regressed:
        changes.update(case.break_node(OPTIONAL))
    case.publish(changes, excluded=[KEEP_FILE])
    cached = case.invoke()
    assert case.base_ids == []
    for side in ("base", "head"):
        assert KEEP not in cached[side]["collected_ids"] and ALREADY_FAILED not in cached[side]["collected_ids"]
    full = case.invoke(cache=False)
    same_verdict(cached, full)
    assert verdict(cached) == ([OPTIONAL] if regressed else [])


def test_node_results_remain_reusable_by_another_pr_shard(shard_case):
    """Reject producer shard/count being used as the identity of otherwise covered node results."""
    case = shard_case
    case.produce()
    cached = case.invoke(shard=1)
    assert case.base_ids == [] and CROSS in cached["base"]["passing_ids"]
    full = case.invoke(shard=1, cache=False)
    assert case.base_ids == [CROSS]
    same_verdict(cached, full)


@pytest.mark.parametrize("change", ["missing", "base-sha"])
def test_missing_or_stale_base_runs_as_today_without_pr_publication(shard_case, change):
    """Reject stale-SHA reuse, publishing PR misses and ignoring a later trusted replacement."""
    case = shard_case
    case.produce()
    if change == "missing":
        case.cache.unlink()
    else:
        case.base = case.head
        case.publish({"base-moved.txt": "new base\n"})
    previous = case.cache.read_bytes() if case.cache.exists() else None
    missed = case.invoke()
    assert KEEP in case.base_ids and OPTIONAL in case.base_ids
    assert (case.cache.read_bytes() if case.cache.exists() else None) == previous
    case.produce()
    reused = case.invoke()
    assert case.base_ids == [] and reused["base"] == missed["base"]


@pytest.mark.parametrize("field,value", [("acceptance_dir", "other/acceptance"),
                                         ("command", "python -m pytest -p foreign_plugin"),
                                         ("marker", "local_only")])
def test_non_exclusion_selection_inputs_still_bind_the_record(shard_case, field, value):
    """Reject dropping directory, normalized command or marker validation when removing exclusion identity."""
    case = shard_case
    case.produce()
    restored = json.loads(case.cache.read_text())
    restored["scope"][field] = value
    case.cache.write_text(json.dumps(restored))
    stale = case.cache.read_bytes()
    missed = case.invoke()
    assert KEEP in case.base_ids and OPTIONAL in case.base_ids
    assert case.cache.read_bytes() == stale
    case.produce()
    reused = case.invoke()
    assert case.base_ids == [] and reused["base"] == missed["base"]


@pytest.mark.parametrize("damage", ["truncated", "invalid-utf8", "null", "array", "object", "unreadable"])
def test_corrupt_or_unreadable_record_misses_without_pr_publication(shard_case, monkeypatch, damage):
    """Reject malformed-record hits, read-error crashes and a PR publishing its own repair."""
    case = shard_case
    case.produce()
    corrupt = {"truncated": b'{"unfinished":', "invalid-utf8": b"\xff\xfe",
               "null": b"null", "array": b"[]", "object": b"{}"}
    context = monkeypatch.context() if damage == "unreadable" else nullcontext()
    with context as patch:
        if damage == "unreadable":
            def refuse_reads(original, *, low_level=False):
                def open_file(path, *args, **kwargs):
                    same_file = isinstance(path, (str, bytes, os.PathLike)) and os.fsdecode(path) == str(case.cache)
                    mode = kwargs.get("flags", args[0] if args else 0) if low_level else kwargs.get("mode", args[0] if args else "r")
                    reading = mode & os.O_ACCMODE != os.O_WRONLY if low_level else "r" in mode or "+" in mode
                    if same_file and reading:
                        raise PermissionError("offline restored-cache read failure")
                    return original(path, *args, **kwargs)
                return open_file
            patch.setattr(builtins, "open", refuse_reads(builtins.open))
            patch.setattr(io, "open", refuse_reads(io.open))
            patch.setattr(os, "open", refuse_reads(os.open, low_level=True))
        else:
            case.cache.write_bytes(corrupt[damage])
        previous = case.cache.read_bytes() if damage != "unreadable" else None
        recovered = case.invoke()
    assert KEEP in case.base_ids and OPTIONAL in case.base_ids
    if previous is not None:
        assert case.cache.read_bytes() == previous
    case.produce()
    reused = case.invoke()
    assert case.base_ids == [] and reused["base"] == recovered["base"]


@pytest.mark.parametrize("number", [101, 202])
def test_pr_misses_remain_uncached_until_the_base_branch_produces(shard_case, number):
    """Reject PR-local base results being published or reused as a trusted shared record."""
    case = shard_case
    for _ in range(2):
        case.invoke(number=number)
        assert KEEP in case.base_ids and OPTIONAL in case.base_ids
        assert not case.cache.exists()
    case.produce()
    case.invoke(number=number)
    assert case.base_ids == []


@pytest.mark.parametrize("source_ref", ["refs/pull/101/merge", "refs/pull/202/merge"])
def test_producer_input_cannot_promote_a_pr_scope_to_a_trusted_record(shard_case, source_ref):
    """Reject trusting a producer flag or PR-written record instead of the base-branch run context."""
    case = shard_case
    case.produce(event="pull_request", source_ref=source_ref)
    case.invoke()
    assert KEEP in case.base_ids and not case.cache.exists()
    case.produce()
    case.invoke()
    assert case.base_ids == []
