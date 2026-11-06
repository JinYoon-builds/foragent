"""Acceptance: a wide issue (expected files above the threshold, default 10) is refused at publish."""

import json
import re

import pytest

from test_order import plan, publishing  # noqa: F401
from test_order_preread import preread, preread_case  # noqa: F401
from test_skills_autosync import cli_exit, isolated_home  # noqa: F401


def answer(files, source=5, tests=5):
    return ("APPROACH: Reuse the confirmation flow.\n"
            f"SIZE: source={source} tests={tests} files={files}")


def publish(case, capsys, *extra):
    capsys.readouterr()
    code = cli_exit(case.main, ["order", "publish", str(case.path), *extra, "--repo-root", str(case.root)])
    captured = capsys.readouterr()
    return code, captured.out + captured.err


def children(case, plan_value):
    titles = {item["title"] for item in plan_value["issues"]}
    return [i for i in case.tracker.list_issues() if i.title in titles]


def assert_wide_refused(case, capsys, count, *extra):
    """The wide issue 'api' (title 'Login API') is refused, the message names it and the count."""
    before = {i.number for i in case.tracker.list_issues()}
    code, message = publish(case, capsys, *extra)
    assert code != 0, f"publishing an issue expecting {count} files must be refused:\n{message}"
    assert "Login API" in message or re.search(r"(?<![\w-])api(?![\w-])", message), message
    assert re.search(rf"(?<!\d){count}(?!\d)", message), message
    after = {i.number for i in case.tracker.list_issues()}
    assert after == before, "a refused publish must not create issues"
    assert not children(case, case.plan) or all("approved" not in i.labels for i in children(case, case.plan))


@pytest.mark.parametrize("count", [11, 12, 32])
@pytest.mark.parametrize("approve", [False, True], ids=["plain", "approve"])
def test_wide_issue_publish_is_refused_naming_issue_and_count(preread_case, capsys, count, approve):
    case = preread_case
    preread(case, capsys, answer(count), "NONE")
    assert_wide_refused(case, capsys, count, *(["--approve"] if approve else []))
    assert children(case, case.plan) == []


def test_mechanical_moves_count_like_other_changes(preread_case, capsys):
    """A wide issue of pure moves (no source or test lines) is still refused by file count."""
    case = preread_case
    preread(case, capsys, answer(32, source=0, tests=0), "NONE")
    assert_wide_refused(case, capsys, 32, "--approve")


@pytest.mark.parametrize("parts", [(6, 5), (10, 10), (4, 4, 3)], ids=["6+5", "10+10", "4+4+3"])
def test_same_issue_split_under_threshold_publishes(preread_case, capsys, parts):
    case = preread_case
    total = sum(parts)
    preread(case, capsys, answer(total), "NONE")
    assert_wide_refused(case, capsys, total, "--approve")

    wide = case.plan["issues"][0]
    split = []
    for index, files in enumerate(parts):
        part = json.loads(json.dumps(wide))
        part["key"] = "api" if index == 0 else f"api-part-{index + 1}"
        part["title"] = f"Login API part {index + 1}"
        part["goal"] = f"Implement login, part {index + 1}."
        if index:
            part["acceptance_files"] = {}
            part["done_when"] = ["true"]
        split.append(part)
    case.plan["issues"] = [*split, *case.plan["issues"][1:]]
    case.plan["issues"][-1]["blocked_by"] = [p["key"] for p in split]
    case.path.write_text(json.dumps(case.plan), encoding="utf-8")

    preread(case, capsys, *[answer(files) for files in parts], "NONE")
    code, message = publish(case, capsys, "--approve")
    assert code == 0, message
    published = children(case, case.plan)
    assert len(published) == len(case.plan["issues"]), message
    assert all("approved" in i.labels for i in published)
    assert not any(i.title == "Login API" for i in case.tracker.list_issues())


@pytest.mark.parametrize("count", [0, 4, 10])
def test_issues_under_threshold_publish_as_today(preread_case, capsys, count):
    """Workflow: the wide estimate is refused first; the same issue estimated at or under 10 publishes."""
    case = preread_case
    preread(case, capsys, answer(11), "NONE")
    assert_wide_refused(case, capsys, 11)

    preread(case, capsys, answer(count), "NONE")
    code, message = publish(case, capsys)
    assert code == 0, message
    code, message = publish(case, capsys, "--approve")
    assert code == 0, message
    published = children(case, case.plan)
    assert len(published) == 2 and all("approved" in i.labels for i in published), message


def test_unknown_estimate_is_not_refused_after_a_wide_refusal(preread_case, capsys):
    """An issue without a SIZE line keeps today's publication; only a known wide count refuses."""
    case = preread_case
    preread(case, capsys, answer(11), "NONE")
    assert_wide_refused(case, capsys, 11)
    preread(case, capsys, "APPROACH: Reuse the confirmation flow.", "NONE")
    code, message = publish(case, capsys, "--approve")
    assert code == 0, message
    assert len(children(case, case.plan)) == 2
