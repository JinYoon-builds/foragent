"""Preparation hands exact reviewed bytes to the existing approval lock."""

import json

import pytest

from conftest import git_command
from lane_tests_case import (CANDIDATE, STATEMENT, TEST_PATH, assert_no_publication,
                             disk_plan, invoke, lane_case, offline_publication, prepare, publish)


def test_reviewed_plan_approves_without_a_second_preread_or_model_run(lane_case, capsys):
    case = lane_case
    prepare(case, capsys)
    before = len(case.calls)
    code, out, err = publish(case, capsys, "--approve")
    assert code == 0, out + err
    assert len(case.calls) == before
    children = [issue for issue in case.tracker.list_issues() if issue.title == "Notice delivery"]
    assert len(children) == 1 and "approved" in children[0].labels
    assert STATEMENT in children[0].body and TEST_PATH in children[0].body
    assert (case.root / TEST_PATH).read_text() == CANDIDATE
    parent = next(issue for issue in case.tracker.list_issues() if issue.title == "Send notices")
    blobs = case.git.blob_shas(case.git.head_sha(), [TEST_PATH])
    assert blobs[TEST_PATH] != "missing" and blobs[TEST_PATH] in parent.body
    assert len([call for call in case.git_calls if "commit" in call]) == 1
    assert len([call for call in case.git_calls if call[0] == "push"]) == 1


@pytest.mark.parametrize("changed", ["statements", "acceptance_files", "done_when", "lane", "reviewer", "identity"])
def test_review_cannot_be_reused_after_inputs_change(lane_case, capsys, monkeypatch, changed):
    case = lane_case
    prepare(case, capsys)
    case.plan = disk_plan(case)
    item = case.plan["issues"][0]
    if changed == "statements":
        item[changed] = ["Send two notices and keep both receipts."]
    elif changed == "acceptance_files":
        item[changed][TEST_PATH] += "# changed since review\n"
    elif changed == "done_when":
        item[changed] = ["true"]
    elif changed == "lane":
        item[changed] = "main"
    elif changed == "identity":
        item["key"] = "a-different-issue"
    else:
        from dataclasses import replace
        from foragent import cli
        case.config = replace(case.config, reviewer_model="changed-reviewer-model")
        monkeypatch.setattr(cli, "load_config", lambda root: case.config)
    case.save()
    code, out, err = publish(case, capsys, "--approve")
    assert code != 0 and "review" in (out + err).lower(), out + err
    assert_no_publication(case)


def test_base_moving_after_review_requires_new_red_evidence(lane_case, capsys, git_history):
    case = lane_case
    prepare(case, capsys)
    git_history.update({"unrelated.txt": "The base moved.\n"})
    git_command(case.root, "fetch", "origin", "main")
    git_command(case.root, "update-ref", "refs/heads/main", git_history.tip)
    code, out, err = publish(case, capsys, "--approve")
    assert code != 0 and "prepare" in (out + err).lower(), out + err
    assert_no_publication(case)


def test_mixed_plan_keeps_old_acceptance_files_and_requires_its_clean_preread(lane_case, capsys):
    case = lane_case
    legacy_path = "tests/acceptance/test_existing_notice.py"
    legacy = {"key": "legacy", "title": "Legacy notice", "goal": "Keep the old contract.",
              "out_of_scope": "New delivery.", "done_when": [f"python {legacy_path}"],
              "lane": "api", "size": "s", "acceptance_files": {legacy_path: "assert True\n"}}
    case.plan["issues"].append(legacy)
    case.save()
    prepare(case, capsys)
    assert disk_plan(case)["issues"][1] == legacy
    # Missing legacy evidence still blocks; preparation does not manufacture it.
    code, out, err = publish(case, capsys, "--approve")
    assert code != 0 and "legacy" in out + err and "preread" in (out + err).lower()
    assert_no_publication(case)
    code, out, err = invoke(case, capsys, "preread", str(case.path), "--json")
    assert code == 0, out + err
    assert [call["role"] for call in case.calls].count("legacy") == 1
    before = len(case.calls)
    code, out, err = publish(case, capsys, "--approve")
    assert code == 0, out + err
    assert len(case.calls) == before
    assert (case.root / legacy_path).read_text() == legacy["acceptance_files"][legacy_path]


def test_empty_legacy_files_cannot_bypass_test_preparation_with_a_clean_none(lane_case, capsys):
    case = lane_case
    del case.plan["issues"][0]["statements"]
    case.plan["issues"][0]["acceptance_files"] = {}
    case.save()
    code, out, err = invoke(case, capsys, "preread", str(case.path), "--json")
    assert code == 0, out + err
    assert json.loads(out)["issues"][0]["unknowns"] == []
    code, out, err = publish(case, capsys, "--approve")
    assert code != 0, "A clean read cannot approve an issue with no locked test files"
    assert "test" in (out + err).lower() and "notice" in out + err
    assert_no_publication(case)
