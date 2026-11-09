"""Offline CI world for #2772 r1320: source modules under src/ that the harness runs.

Builds on pr_ci_world (read-only): the same demo project, this repository's committed
workflows and `fa ci workflow` output, simulated offline. The demo project here also has
source modules that are not application code but part of its test or CI harness:

- `src/demo/pytest_marks.py`: a pytest plugin the test configuration loads with
  `addopts = "-p demo.pytest_marks"` in pyproject.toml;
- `src/demo/recorder.py`: a pytest plugin the root conftest.py loads with
  `pytest_plugins = ["demo.recorder"]`;
- `src/demo/recorder_support.py`: a helper only that plugin imports (the harness runs it
  too, one step removed);
- `src/demo/ci_report.py`: a module a committed CI workflow step runs
  (`uv run python -m demo.ci_report`), like a project's own measurement or sharding runner.

No test file imports any of them, so the changed-file relation alone maps a change to
them onto no test at all. Nothing reaches the network and no signal is sent.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
import yaml

from foragent import cli
from foragent.ci_workflow import WORKFLOW
from pr_ci_world import ROOT, Scenarios, World, project_files
from test_local_truth import record_gh
from test_skills_autosync import cli_exit


PLUGIN_ADDOPTS = "src/demo/pytest_marks.py"
PLUGIN_CONFTEST = "src/demo/recorder.py"
PLUGIN_SUPPORT = "src/demo/recorder_support.py"
CI_STEP_MODULE = "src/demo/ci_report.py"
REPORT_WORKFLOW = ".github/workflows/report.yml"


def harness_files():
    return {
        "pyproject.toml": ('[project]\nname = "demo"\nversion = "0.0.0"\n\n'
                           '[tool.pytest.ini_options]\npythonpath = ["src"]\n'
                           'addopts = "-p demo.pytest_marks"\n'
                           'markers = ["local_only: requires machine tools; run locally"]\n'),
        "conftest.py": ('# Root test harness of the demo project.\n'
                        'pytest_plugins = ["demo.recorder"]\n'),
        PLUGIN_ADDOPTS: ("def pytest_configure(config):\n"
                         "    config.addinivalue_line('markers', 'demo_mark: marker from the demo plugin')\n"),
        PLUGIN_CONFTEST: ("from demo import recorder_support\n\n\n"
                          "def pytest_report_header(config):\n"
                          "    return recorder_support.header()\n"),
        PLUGIN_SUPPORT: "def header():\n    return 'demo recorder'\n",
        CI_STEP_MODULE: ("import sys\n\n\n"
                         "def main():\n    print('demo CI report')\n    return 0\n\n\n"
                         "if __name__ == '__main__':\n    sys.exit(main())\n"),
        REPORT_WORKFLOW: ("name: report\n"
                          "on:\n  pull_request:\n  push:\n    branches: [main]\n"
                          "jobs:\n  report:\n    runs-on: ubuntu-latest\n    steps:\n"
                          "      - uses: actions/checkout@v4\n"
                          "      - run: uv run python -m demo.ci_report\n"),
    }


class HarnessWorld(World):
    """pr_ci_world.World with the demo project's harness modules added to its base commit."""

    def _build(self):
        files = {**project_files(self.log_file), **harness_files()}
        for relative, text in files.items():
            path = self.project / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        config = yaml.safe_load((ROOT / ".foragent.yml").read_text(encoding="utf-8"))
        config.update(base_branch="main", ci_shards=2, checks="ci", test_command="uv run pytest -q")
        (self.project / ".foragent.yml").write_text(yaml.safe_dump(config), encoding="utf-8")
        workflows = self.project / ".github/workflows"
        workflows.mkdir(parents=True, exist_ok=True)
        for source in sorted((ROOT / ".github/workflows").glob("*.y*ml")):
            if source.name != Path(WORKFLOW).name:
                shutil.copyfile(source, workflows / source.name)
        with pytest.MonkeyPatch.context() as patch:
            record_gh(patch, lambda argv: json.dumps({"isPrivate": False}))
            code = cli_exit(cli.main, ["ci", "workflow", "--repo-root", str(self.project)])
        assert code == 0, "`fa ci workflow` must generate the project's CI"
        self.git("init", "-q", "-b", "main")
        self.base = self._snapshot([], "Demo project with its CI and harness modules")
        self.git("update-ref", "refs/heads/main", self.base)
        self.git("remote", "add", "origin", str(self.project))
        self.git("update-ref", "refs/remotes/origin/main", self.base)


class HarnessScenarios(Scenarios):
    """Scenarios whose every run builds a HarnessWorld."""

    def run(self, kind, changes, label):
        key = (kind, label)
        if key not in self.cache:
            world = HarnessWorld(self.factory.mktemp(f"harness-{kind}-{label}"))
            event = {"lane": world.lane_pr, "train": world.train_pr, "push": world.main_push}[kind](changes)
            outcome = world.simulate(event, kind)
            failure = getattr(world, "_last_failure", None)
            if failure:
                outcome.log.append(failure)
            self.cache[key] = outcome
        return self.cache[key]


def _edit(note):
    return lambda text: text + f"\n# Lane edit: {note}.\n"


TOUCH_PLUGIN_ADDOPTS = {PLUGIN_ADDOPTS: _edit("pytest plugin loaded by addopts")}
TOUCH_PLUGIN_CONFTEST = {PLUGIN_CONFTEST: _edit("pytest plugin loaded by conftest pytest_plugins")}
TOUCH_PLUGIN_SUPPORT = {PLUGIN_SUPPORT: _edit("module only the pytest plugin imports")}
TOUCH_CI_STEP_MODULE = {CI_STEP_MODULE: _edit("module a CI workflow step runs")}
