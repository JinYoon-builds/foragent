"""Acceptance test for base-move-neutral-more. Locked by Foragent; do not edit."""

import pytest
import yaml

from conftest import git_command
from foragent.issue import body_hash
from test_base_move_reuse import (  # noqa: F401
    REUSED, REVERIFY, assert_verifications, locked_test, move_during_publish,
    publish_case, run_to_merge,
)
from test_skills_autosync import isolated_home  # noqa: F401
from test_state_split import offline_acceptance  # noqa: F401


NEW_ORDER = "tests/acceptance/test_new_order.py"


@pytest.fixture
def config_case(publish_case):
    case = publish_case
    case.base_config = {
        "repo": "owner/repo", "review": "fast",
        "test_command": case.config.test_command,
        "max_runtime_minutes": 90, "stall_minutes": 30,
    }
    case.history.update({".foragent.yml": yaml.safe_dump(case.base_config)})
    git_command(case.root, "fetch", "-q", "origin")
    git_command(case.root, "merge", "-q", "--ff-only", "origin/main")
    case.base_sha = case.history.tip
    return case


def publish_new_order(case, monkeypatch, *, ownership="approval", with_source=False):
    """Publish the other issue and its locked file after initial verification."""
    assert git_command(case.history.origin, "ls-tree", case.base_sha, "--", NEW_ORDER) == "", (
        "The new order's file must not exist on this run's original baseline"
    )
    files = {NEW_ORDER: locked_test("new_order", passing=False)}
    if with_source:
        files["src/widget.py"] = "VALUE = 1\n"
    move_during_publish(case, monkeypatch, [[files]])
    review = case.reviewer.steps[0]

    def review_and_publish(kwargs):
        verdict = review(kwargs)
        body = (
            "<!-- foragent:v1 -->\n## Goal\nImplement another order.\n"
            "## Out of scope\nThe current feature.\n"
        )
        if ownership == "files":
            body += f"## Files\n- {NEW_ORDER}\n"
        command = f"python -m pytest -q {NEW_ORDER}" if ownership == "acceptance-link" else "true"
        body += f"## Done when\n```sh\n{command}\n```\n"
        case.tracker.add_issue(4, "Implement new order", body, labels=["approved"])
        if ownership == "approval":
            case.store.save_approval(
                4, body=body, body_sha=body_hash(body),
                acceptance_blobs={NEW_ORDER: git_command(
                    case.history.origin, "rev-parse", f"{case.history.tip}:{NEW_ORDER}",
                )},
                approved_by="human", approved_at=case.clock[0].isoformat(),
            )
        return verdict

    case.reviewer.steps = [review_and_publish]


def test_runtime_and_stall_config_changes_reuse_verification(config_case, monkeypatch):
    case = config_case
    changed = {**case.base_config, "max_runtime_minutes": 360, "stall_minutes": 60}
    move_during_publish(case, monkeypatch, [[{".foragent.yml": yaml.safe_dump(changed)}]])

    log = run_to_merge(case, moves=1)

    assert log.count(REUSED) == 1, "Runtime/stall-only config changes unnecessarily re-verified"
    assert REVERIFY not in log
    assert_verifications(case, [case.base_sha])


def test_test_command_config_change_reverifies(config_case, monkeypatch):
    case = config_case
    changed = {**case.base_config, "test_command": case.config.test_command + " tests/test_widget.py"}
    move_during_publish(case, monkeypatch, [[{".foragent.yml": yaml.safe_dump(changed)}]])

    log = run_to_merge(case, moves=1)

    assert_verifications(case, [case.base_sha, case.moves[0]])
    assert log.count(REVERIFY) == 1, "Changing what the check runs must re-verify"
    assert REUSED not in log


@pytest.mark.parametrize("ownership", ["approval", "files", "acceptance-link"])
def test_new_open_orders_locked_acceptance_reuses_verification(
    publish_case, monkeypatch, ownership,
):
    case = publish_case
    publish_new_order(case, monkeypatch, ownership=ownership)

    log = run_to_merge(case, moves=1)

    assert case.tracker.get_issue(4).state == "open"
    assert NEW_ORDER not in git_command(
        case.history.origin, "diff", "--name-only", case.moves[0], "refs/heads/foragent/issue-1",
    ).splitlines(), "The running branch must not touch the new order's locked file"
    assert log.count(REUSED) == 1, (
        f"A new locked file owned through {ownership} was never protected by this run; re-verified"
    )
    assert REVERIFY not in log
    assert_verifications(case, [case.base_sha])


def test_new_locked_acceptance_and_source_move_reverifies(publish_case, monkeypatch):
    case = publish_case
    publish_new_order(case, monkeypatch, with_source=True)

    log = run_to_merge(case, moves=1)

    assert_verifications(case, [case.base_sha, case.moves[0]])
    assert log.count(REVERIFY) == 1, "A mixed move containing source changes must re-verify"
    assert REUSED not in log


def test_unparsable_config_on_main_reverifies(config_case, monkeypatch):
    case = config_case
    invalid = yaml.safe_dump(case.base_config) + "run_hours: [\n"
    with pytest.raises(yaml.YAMLError):
        yaml.safe_load(invalid)
    move_during_publish(case, monkeypatch, [[{".foragent.yml": invalid}]])

    log = run_to_merge(case, moves=1)

    assert_verifications(case, [case.base_sha, case.moves[0]])
    assert log.count(REVERIFY) == 1, "Unparsable config must re-verify even for a neutral key"
    assert REUSED not in log
