"""Bound actual added input for each role, independently of other role tables."""
import json
import pytest
from test_default_skill_sessions import ROLE_NAMES, run_consumer, skill_files
from test_skills_autosync import isolated_home  # noqa: F401

TABLE = {role: tuple(f"fixture-size-{role}-{n:02d}" for n in range(12)) for role in ROLE_NAMES}
BOUND = 8 * 1024


def text_bytes(call):
    if "argv" in call:
        return sum(len(value.encode()) for value in call["argv"])
    return sum(len(value.encode()) for value in call.values() if isinstance(value, str))


@pytest.mark.parametrize("consumer,executor", [
    *[(role, ex) for role in ("workers", "no-lane", "reviewers", "docs", "architect") for ex in ("codex", "claude")],
    ("orchestrator", "claude"),
])
def test_role_default_prompt_addition_stays_within_budget(git_history, tmp_path, monkeypatch, consumer, executor):
    from test_risk_map import copy_history
    role = "workers" if consumer == "no-lane" else consumer
    installed = tuple(name for names in TABLE.values() for name in names)
    paired = []
    for label, enabled in (("bare", False), ("with", True)):
        root = tmp_path / label
        history = copy_history(git_history, root)
        history.update(skill_files(installed))
        table = dict(TABLE)
        if not enabled:
            table.pop(role)
        with monkeypatch.context() as mp:
            paired.append(run_consumer(consumer, executor, history, root, mp, table,
                                       observe=text_bytes, available=installed))
    before, after = paired
    assert before and len(before) == len(after)
    additions = [b - a for a, b in zip(before, after)]
    assert all(0 < size <= BOUND for size in additions)
    print("SKILL_SIZE " + json.dumps({"role": role, "consumer": consumer, "executor": executor,
        "defaults_for_this_role": 12, "other_role_defaults": 48,
        "before_max_bytes": max(before), "after_max_bytes": max(after),
        "added_max_bytes": max(additions), "bound_bytes": BOUND}))
