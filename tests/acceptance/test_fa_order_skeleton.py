"""Acceptance test for fa-order-skeleton. Locked by Foragent; do not edit."""

from pathlib import Path
import re


def split_text():
    path = Path(__file__).resolve().parents[2] / "skills/fa-order/SKILL.md"
    section = re.search(r"^## 4\. Split into issues\n(.*?)(?=^## |\Z)",
                        path.read_text(encoding="utf-8"), re.M | re.S)
    assert section, "Keep the skeleton guidance in step 4, where issues are split"
    return " ".join(section[1].casefold().split())


def test_fa_order_skeleton_precedes_issues_that_depend_on_each_others_code():
    assert re.search(
        r"(?:when|if).{0,40}(?:several|multiple) issues.{0,40}depend on each other['’]s code"
        r".{0,100}shared interfaces.{0,60}one screen.{0,30}parts"
        r".{0,80}first issue.{0,30}skeleton",
        split_text(),
    ), "Shared code dependencies need a first skeleton issue, including a screen split into parts"


def test_fa_order_skeleton_defines_shared_contracts_in_code_and_in_the_order_spec():
    text = split_text()
    assert re.search(
        r"skeleton.{0,40}defines.{0,30}shared names.{0,30}signatures"
        r".{0,30}data shapes.{0,20}in code",
        text,
    ), "The skeleton must define names, signatures and data shapes in code"
    assert re.search(
        r"order spec.{0,25}lists.{0,30}(?:them|shared names).{0,30}under"
        r".{0,20}contracts.{0,15}heading",
        text,
    ), "List the same shared contracts under the order spec's Contracts heading"


def test_fa_order_skeleton_is_a_blocked_by_dependency_of_every_dependent_issue():
    assert re.search(
        r"every dependent issue.{0,25}lists.{0,25}skeleton.{0,30}blocked_by",
        split_text(),
    ), "Every dependent issue must name the skeleton in blocked_by"
