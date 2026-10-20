from dataclasses import replace

import pytest

from foragent.config import Config
from foragent.policy import merge_decision, review_mode_for, touched
from foragent.reasons import Reason


@pytest.mark.parametrize("pattern,expected", [
    ("src/**", ["src/a.py", "src/nested/b.py"]),
    ("src/*.py", ["src/a.py"]),
    ("**/*.py", ["root.py", "src/a.py", "src/nested/b.py"]),
    ("src/**/b.?y", ["src/nested/b.py"]),
    ("**", ["root.py", "src/a.py", "src/nested/b.py", "docs/a.md"]),
    ("**/a.[pm][yd]", ["src/a.py", "docs/a.md"]),
])
def test_touched_segment_globs_and_recursive_globs(pattern, expected):
    paths = ["root.py", "src/a.py", "src/nested/b.py", "docs/a.md"]
    assert touched(paths, [pattern]) == expected
    assert touched(paths, []) == []


def test_recursive_glob_matches_zero_directories_and_deduplicates():
    assert touched(["src/a.py"], ["src/**/a.py", "**/*.py"]) == ["src/a.py"]


def test_review_mode_uses_strictest_match_including_default():
    config = Config(review="fast", review_paths={"src/**": "batch", "**/auth/**": "careful"})
    assert review_mode_for([], config) == "fast"
    assert review_mode_for(["src/a.py"], config) == "batch"
    assert review_mode_for(["src/a.py", "auth/login.py"], config) == "careful"
    assert review_mode_for(["src/a.py"], replace(config, review="careful")) == "careful"
    assert review_mode_for(["src/a.py"], Config(review="batch", review_paths={"src/**": "fast"})) == "batch"


@pytest.mark.parametrize("paths,labels,config,kind,reason", [
    (["app.py"], [], Config(review="fast"), "merge", None),
    (["tests/acceptance/nested/test.py"], [], Config(review="fast"), "hold", Reason.ACCEPTANCE_TOUCHED),
    (["tests/acceptance-other/test.py"], [], Config(review="fast"), "merge", None),
    (["accept/test.sh"], [], Config(review="fast", acceptance_dir="./accept"), "hold", Reason.ACCEPTANCE_TOUCHED),
    (["auth/login.py"], [], Config(review="fast", always_human=["auth/**"]), "hold", Reason.ALWAYS_HUMAN_PATH),
    (["app.py"], ["risk:high"], Config(review="fast"), "hold", Reason.RISK_HIGH),
    (["app.py"], [], Config(review="batch"), "hold_mode", None),
    (["src/a.py"], [], Config(review="fast", review_paths={"src/**": "careful"}), "hold_mode", None),
    (["tests/acceptance/a.py"], [], Config(review="careful"), "hold", Reason.ACCEPTANCE_TOUCHED),
])
def test_merge_decision(paths, labels, config, kind, reason):
    decision = merge_decision(changed_paths=paths, issue_labels=labels, config=config)
    assert decision.kind == kind and decision.reason == reason
    if kind == "hold_mode":
        assert decision.summary == f"review mode {decision.mode} not implemented yet; PR left open"
