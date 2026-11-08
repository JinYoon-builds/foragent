"""Generated workflows stay synchronized without replacing team-owned CI."""

from pathlib import Path

import pytest
import yaml

from foragent.ci_workflow import UNIT_WORKFLOW, WORKFLOW, unit_workflow_text, workflow_text, write_workflow
from foragent.config import Config, load_config


def test_both_committed_workflows_match_generator(tmp_path):
    root = Path(__file__).resolve().parents[1]
    config = load_config(root, global_path=root / "absent-global.yml")
    runner = tmp_path / "src/foragent/ci_unit_shard.py"
    runner.parent.mkdir(parents=True)
    runner.write_text("# Project provides the unit shard entry point.\n")
    write_workflow(tmp_path, config, warn=False)
    for name in (UNIT_WORKFLOW, WORKFLOW):
        assert (root / name).read_text() == (tmp_path / name).read_text()


def test_generator_retains_an_existing_team_unit_workflow(tmp_path):
    path = tmp_path / UNIT_WORKFLOW
    path.parent.mkdir(parents=True)
    original = "name: team-ci\non: [push]\njobs: {}\n"
    path.write_text(original)
    write_workflow(tmp_path, Config(), warn=False)
    assert path.read_text() == original


def test_generator_does_not_add_a_unit_runner_that_the_project_does_not_provide(tmp_path):
    write_workflow(tmp_path, Config(), warn=False)
    assert not (tmp_path / UNIT_WORKFLOW).exists()
    assert (tmp_path / WORKFLOW).is_file()


@pytest.mark.parametrize("changed,runs", [
    (["tests/acceptance/test_new_order.py"], False),
    (["docs/orders.md", ".claude/settings.json", "README.md", "tests/acceptance/test_new_order.py"], False),
    (["docs/design.txt"], False),
    ([".claude/settings.json"], False),
    (["README.md"], False),
    (["src/README.md", "tests/README.md", ".github/README.md", "scripts/README.md"], False),
    (["src/foragent/runner.py"], True),
    (["tests/conftest.py"], True),
    (["tests/acceptance/conftest.py"], True),
    (["tests/nested/conftest.py"], True),
    (["tests/acceptance/team_v1_support.py"], True),
    (["tests/acceptance/test_support.py"], False),
    (["tests/acceptance/pr_ci_world.py"], True),
    (["tests/acceptance/helpers/test_tools.py"], True),
    (["tests/test_runner.py"], True),
    (["pyproject.toml"], True),
    (["setup.py"], True),
    ([".python-version"], True),
    (["new-input.custom"], True),
    (["uv.lock"], True),
    ([".github/workflows/ci.yml"], True),
    ([".foragent.yml"], True),
    (["scripts/shard_times.json"], True),
    (["tests/acceptance/test_new_order.py", "src/foragent/runner.py"], True),
    (["docs/orders.md", "tests/acceptance/conftest.py"], True),
])
def test_generated_workflows_filter_main_pushes(monkeypatch, changed, runs):
    monkeypatch.syspath_prepend(str(Path(__file__).parent / "acceptance"))
    from pr_ci_world import Event, triggered

    event = Event("push", "refs/heads/main", "main", "head", "main", changed, {})
    for generate in (unit_workflow_text, workflow_text):
        document = yaml.safe_load(generate(Config()))
        assert "paths" not in document["on"]["push"]
        assert document["on"]["push"]["paths-ignore"] == [
            "docs/**", ".claude/**", "**/*.md", "tests/acceptance/test_*.py"]
        assert triggered(document, event) is runs


@pytest.mark.parametrize("branch", ["fix/docs", "foragent/issue-7", "foragent/batch-merge-abc-2"])
def test_publication_only_pr_and_train_ci_still_run(monkeypatch, branch):
    monkeypatch.syspath_prepend(str(Path(__file__).parent / "acceptance"))
    from pr_ci_world import Event, triggered

    event = Event("pull_request", "refs/pull/42/merge", "42/merge", "head", "main",
                  ["docs/orders.md", "tests/acceptance/test_new_order.py"], {}, head_ref=branch)
    for generate in (unit_workflow_text, workflow_text):
        document = yaml.safe_load(generate(Config()))
        assert document["on"]["pull_request"] is None
        assert triggered(document, event)


def test_train_concurrency_groups_are_separate_from_member_pr_ci():
    import yaml
    from acceptance.gh_expressions import interpolate

    for generate in (unit_workflow_text, workflow_text):
        document = yaml.safe_load(generate(Config()))

        def group(branch):
            return interpolate(document["concurrency"]["group"], {"github": {
                "workflow": document["name"], "head_ref": branch, "ref": "refs/pull/42/merge",
                "event": {"pull_request": {"number": 42}},
            }})

        assert group("foragent/batch-merge-abc-2") not in {
            group("foragent/issue-7"), group("foragent/group-1-2"), group("fix/cli-output")}
        assert document["concurrency"]["cancel-in-progress"] is True


def test_pr_workflows_run_full_suites_and_catch_subprocess_regressions(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).parent / "acceptance"))
    import pr_ci_world as ci

    project_files = ci.project_files

    def project_with_cli_tests(log):
        files = project_files(log)
        files["src/demo/cli.py"] = "print(1)\n"
        for path in (ci.UNIT_ALPHA, ci.ACCEPT_ALPHA):
            files[path] = ci._test_file(path, "import os\nimport subprocess\nimport sys",
                "assert subprocess.check_output([sys.executable, '-m', 'demo.cli'], text=True, "
                "env={**os.environ, 'PYTHONPATH': 'src'}).strip() == '1'", log)
        return files

    monkeypatch.setattr(ci, "project_files", project_with_cli_tests)
    world = ci.World(tmp_path / "world")
    branch, number = "fix/cli-output", 101
    head, changed = world._commit_change(branch, {"src/demo/cli.py": "print(0)\n"}, "Change CLI output")
    # Ordinary PRs carry no lane exclusions trailer.
    head = world.git("commit-tree", f"{head}^{{tree}}", "-p", world.base, "-m", "Change CLI output")
    world.git("update-ref", f"refs/heads/{branch}", head)
    world._serve(head, changed, number, branch, "", "Change CLI output")
    event = world._pr_event(number, branch, head, changed, "", "Change CLI output")
    outcome = world.simulate(event, "ordinary PR with a subprocess regression")
    assert outcome.jobs["ci.yml:test"] == "failure", outcome.describe()
    assert outcome.jobs["foragent-acceptance.yml:acceptance"] == "failure", outcome.describe()
    assert ci.ALL_TESTS <= outcome.files, outcome.describe()
