"""Acceptance test for fast-checks. Locked by Foragent; do not edit."""

from dataclasses import asdict
from pathlib import Path
import shlex
import xml.etree.ElementTree as ET

from test_acceptance_baseline import FUTURE, KEEP
from test_skills_autosync import isolated_home
from test_state_split import offline_acceptance


FULL_SUITE = "uv run pytest -q"
RELATED = {
    "tests/test_scheduler.py": (
        "from foragent.scheduler import tick\n\n"
        "def test_tick():\n    assert callable(tick)\n"
    ),
    "tests/test_scheduler_import.py": (
        "import foragent.scheduler as schedule\n\n"
        "def test_tick():\n    assert callable(schedule.tick)\n"
    ),
    "tests/unit/test_scheduler_from_package.py": (
        "from foragent import scheduler as schedule\n\n"
        "def test_tick():\n    assert callable(schedule.tick)\n"
    ),
}


def fast_case(git_history, tmp_path, monkeypatch, changes):
    """Real runner and Git diffs; fake only executors, GitHub and check processes."""
    from conftest import ACCEPTANCE, git_command
    from foragent import runner
    from foragent.issue import body_hash, parse_body
    from test_orch_review_handoff import make_case

    git_history.update({
        "feature.txt": "done\n",
        "src/foragent/__init__.py": "",
        "src/foragent/scheduler.py": "def tick(ctx):\n    return ctx\n",
        "src/foragent/bench.py": "VALUE = 1\n",
        **RELATED,
        "tests/test_bench.py": (
            "from foragent import bench\n"
            "# import foragent.scheduler is documentation, not an import.\n"
            'EXAMPLE = "from foragent.scheduler import tick"\n\n'
            "def test_bench():\n    assert bench is not None\n"
        ),
        "tests/test_changed.py": "def test_changed():\n    assert 1 == 1\n",
        "skills/demo/SKILL.md": "# Demo\nOriginal instructions.\n",
        KEEP: "def test_keep():\n    assert True\n",
        FUTURE: "def test_future():\n    assert False\n",
    })
    git_command(git_history.clone, "fetch", "-q", "origin")
    git_command(git_history.clone, "merge", "-q", "--ff-only", "origin/main")
    case = make_case(git_history, tmp_path, monkeypatch,
                     skill_libraries=[], test_command=FULL_SUITE)
    snapshot = asdict(case.store.load_approval(1))
    snapshot["body"] = snapshot["body"].replace(
        f"test -f {ACCEPTANCE}", f"{FULL_SUITE} {ACCEPTANCE}",
    )
    snapshot["body_sha"] = body_hash(snapshot["body"])
    case.store.save_approval(**snapshot)
    case.tracker.edit_body(1, snapshot["body"])
    case.done_commands = parse_body(snapshot["body"]).done_when_commands
    case.baseline_ids = {f"{ACCEPTANCE}::test_feature", f"{KEEP}::test_keep"}
    case.check_commands = []

    def implement(kwargs):
        case.implement(kwargs)
        for relative, content in changes.items():
            path = kwargs["worktree"] / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")

    case.implementer.steps = [implement]

    def check_process(argv, *, cwd, **kwargs):
        assert argv[:2] == ["/bin/sh", "-c"], argv
        command = argv[2]
        case.check_commands.append((command, Path(cwd)))
        words = shlex.split(command)
        report = next((arg.split("=", 1)[1] for arg in words if arg.startswith("--junitxml=")), None)
        if report is not None:
            # D53's initial discovery includes passing tests and one pending issue.
            suite = ET.Element("testsuite")
            for path, name in ((ACCEPTANCE, "test_feature"), (KEEP, "test_keep"),
                               (FUTURE, "test_future")):
                test = ET.SubElement(suite, "testcase", file=path, name=name,
                                     classname=path.removesuffix(".py").replace("/", "."))
                if path == FUTURE:
                    ET.SubElement(test, "failure", message="another issue is not implemented")
            ET.ElementTree(suite).write(report, encoding="unicode")
            return 1, False, None
        return 0, False, None

    # _check remains real: observe the commands it sends to the shell boundary.
    monkeypatch.setattr(runner, "run_logged", check_process)
    return case


def unit_commands_after_acceptance(case):
    """Assert Done-when -> the unchanged D53 baseline before examining units."""
    discovery = [(command, cwd) for command, cwd in case.check_commands if "--junitxml=" in command]
    assert len(discovery) == 1, case.check_commands
    assert discovery[0][1] != case.store.worktree_path(1)
    assert "tests/acceptance/" in shlex.split(discovery[0][0])
    commands = [command for command, cwd in case.check_commands if cwd == case.store.worktree_path(1)]
    assert commands[:len(case.done_commands)] == case.done_commands
    baseline = shlex.split(commands[len(case.done_commands)])
    baseline_ids = {word for word in baseline if word.startswith("tests/")}
    assert baseline_ids == case.baseline_ids
    assert "test_future" not in case.implementer.calls[0]["prompt"]
    assert FULL_SUITE not in commands, f"A bare full suite ran during checking: {commands}"
    return commands[len(case.done_commands) + 1:]


def selected_test_files(commands):
    return {word.split("::", 1)[0] for command in commands
            for word in shlex.split(command) if word.startswith("tests/")}


def test_fast_checks_scheduler_change_runs_static_importers_and_changed_tests_only(
    git_history, tmp_path, monkeypatch,
):
    from foragent.runner import run_issue

    case = fast_case(git_history, tmp_path, monkeypatch, {
        "src/foragent/scheduler.py": "def tick(ctx):\n    return (ctx, 'changed')\n",
        "tests/test_changed.py": "def test_changed():\n    assert 2 == 2\n",
    })
    assert run_issue(1, case.ctx) == "merged", case.store.log_path(1).read_text()
    commands = unit_commands_after_acceptance(case)
    selected = selected_test_files(commands)
    expected = set(RELATED) | {"tests/test_changed.py"}
    assert selected == expected, f"Related unit tests missing or unrelated tests selected: {selected}; expected {expected}"
    assert "tests/test_bench.py" not in selected
    assert f"`{FULL_SUITE}`" in case.implementer.calls[0]["prompt"]


def test_fast_checks_changed_test_is_selected_without_a_source_change(
    git_history, tmp_path, monkeypatch,
):
    from foragent.runner import run_issue

    case = fast_case(git_history, tmp_path, monkeypatch, {
        "tests/test_changed.py": "def test_changed():\n    assert 3 == 3\n",
        "tests/unit/test_added.py": "def test_added():\n    assert True\n",
    })
    assert run_issue(1, case.ctx) == "merged", case.store.log_path(1).read_text()
    commands = unit_commands_after_acceptance(case)
    assert selected_test_files(commands) == {"tests/test_changed.py", "tests/unit/test_added.py"}, (
        f"Modified and newly added test files must run even without a changed module: {commands}"
    )


def test_fast_checks_skill_only_change_runs_done_when_and_baseline_without_units(
    git_history, tmp_path, monkeypatch,
):
    from foragent.runner import run_issue

    case = fast_case(git_history, tmp_path, monkeypatch, {
        "skills/demo/SKILL.md": "# Demo\nClarified instructions.\n",
    })
    assert run_issue(1, case.ctx) == "merged", case.store.log_path(1).read_text()
    assert unit_commands_after_acceptance(case) == []


def test_fast_checks_prompt_still_names_the_optional_full_suite():
    from test_ci_feedback import test_prompt_names_the_full_test_command as scenario

    scenario()


def test_fast_checks_ci_failure_still_returns_to_the_implementer(
    git_history, tmp_path, monkeypatch,
):
    from test_ci_feedback import test_ci_failure_goes_back_to_the_implementer_with_the_log as scenario

    scenario(git_history, tmp_path, monkeypatch)
