"""Shared fixture project for the sharded acceptance-baseline tests (helper only).

A small CI-mode project with known per-node outcomes and committed shard
times. One simulated main push of the generated workflow must publish the
same base cache an unsharded run would: every collected node's outcome.
"""

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
import yaml

from foragent import cli
from foragent.ci_base_cache import read_results
from foragent.ci_pytest import balanced, shard_of
from foragent.ci_workflow import WORKFLOW, measurement_scope
from foragent.config import load_config
from test_local_truth import record_gh
from test_skills_autosync import cli_exit

from base_shards_sim import PushRun, deep_render, condition, lines_of


ROOT = next(parent for parent in Path(__file__).resolve().parents
            if (parent / ".foragent.yml").is_file() and (parent / "src/foragent/ci_shard.py").is_file())
REPOSITORY = "owner/repo"
SHARDS = 6
LOG_VARIABLE = "BASE_SHARDS_EXECUTION_LOG"

# file -> [(test name, outcome)]; outcome passed, failed or skipped.
FILES = {
    "tests/acceptance/test_alpha.py": [("test_one", "passed"), ("test_broken", "failed")],
    "tests/acceptance/test_bravo.py": [("test_one", "passed")],
    "tests/acceptance/test_charlie.py": [("test_skipped", "skipped"), ("test_two", "passed")],
    "tests/acceptance/test_delta.py": [("test_one", "passed")],
    "tests/acceptance/test_echo.py": [("test_one", "failed")],
    "tests/acceptance/test_foxtrot.py": [("test_one", "passed"), ("test_two", "passed")],
    "tests/acceptance/test_golf.py": [("test_one", "passed")],
    "tests/acceptance/test_hotel.py": [("test_one", "passed")],
    # Not in the times file: assigned by the hash fallback, as on PR shards.
    "tests/acceptance/test_untimed.py": [("test_new", "passed")],
}
TIMES = {"tests/acceptance/test_alpha.py": 90.0, "tests/acceptance/test_bravo.py": 80.0,
         "tests/acceptance/test_charlie.py": 70.0, "tests/acceptance/test_delta.py": 60.0,
         "tests/acceptance/test_echo.py": 50.0, "tests/acceptance/test_foxtrot.py": 40.0,
         "tests/acceptance/test_golf.py": 30.0, "tests/acceptance/test_hotel.py": 20.0,
         "tests/acceptance/test_removed.py": 10.0}
ALL_NODES = {f"{path}::{name}" for path, tests in FILES.items() for name, _ in tests}
# What one unsharded production records: every collected node's outcome.
EXPECTED_RESULTS = {f"{path}::{name}": "passed" if outcome == "passed" else "nonpassing"
                    for path, tests in FILES.items() for name, outcome in tests}


def source(tests):
    body = "import json, os\nfrom pathlib import Path\nimport pytest\n\n"
    for name, outcome in tests:
        body += (f"def {name}():\n"
                 f"    with open(os.environ[{LOG_VARIABLE!r}], 'a') as stream:\n"
                 f"        stream.write(json.dumps({{'cwd': os.getcwd(), 'test': {name!r},"
                 f" 'file': __file__}}) + '\\n')\n")
        body += {"passed": "    assert True\n", "failed": "    assert False, 'known failure'\n",
                 "skipped": "    pytest.skip('known skip')\n"}[outcome] + "\n"
    return body


def git(root, *args):
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True).stdout.strip()


def expected_partition():
    """The PR acceptance shards' assignment: base times of present files, hash fallback."""
    present = {path: seconds for path, seconds in TIMES.items() if path in FILES}
    assignment = balanced(present, SHARDS)
    shards = {}
    for path, tests in FILES.items():
        index = shard_of(path, SHARDS, assignment)
        shards.setdefault(index, set()).update(f"{path}::{name}" for name, _ in tests)
    return shards


class Project:
    def __init__(self, base):
        self.base = Path(base)
        self.origin = self.base / "origin"
        self.origin.mkdir(parents=True)
        config = {"checks": "ci", "ci_shards": SHARDS,
                  "test_command": f"{sys.executable} -m pytest -p no:cacheprovider"}
        files = {".foragent.yml": yaml.safe_dump(config),
                 "scripts/shard_times.json": json.dumps(TIMES, sort_keys=True),
                 **{path: source(tests) for path, tests in FILES.items()}}
        git(self.origin, "init", "-q", "--bare", "-b", "main")
        message = "Base\n"
        stream = ["commit refs/heads/main\n", "committer Offline Tests <tests@example.com> 1700000000 +0000\n",
                  f"data {len(message.encode())}\n{message}\n"]
        for path, content in files.items():
            stream.append(f"M 100644 inline {path}\ndata {len(content.encode())}\n{content}\n")
        stream.append("\ndone\n")
        subprocess.run(["git", "fast-import", "--quiet"], cwd=self.origin, input="".join(stream),
                       check=True, capture_output=True, text=True)
        self.sha = git(self.origin, "rev-parse", "refs/heads/main")
        self.checkout = self.base / "checkout"
        git(self.base, "clone", "-q", str(self.origin), str(self.checkout))
        self.config = load_config(self.checkout, global_path=self.base / "no-global-config")
        self.scope = measurement_scope(self.config, self.checkout, [])
        self.serial = 0

    def generated_workflow(self):
        """`fa ci workflow` output for this project's configuration (the public generator)."""
        target = self.base / "generator"
        target.mkdir(exist_ok=True)
        (target / ".foragent.yml").write_text((self.checkout / ".foragent.yml").read_text(), encoding="utf-8")
        with pytest.MonkeyPatch.context() as patch:
            record_gh(patch, lambda argv: json.dumps({"isPrivate": False}))
            assert cli_exit(cli.main, ["ci", "workflow", "--repo-root", str(target)]) == 0
        return yaml.safe_load((target / WORKFLOW).read_text(encoding="utf-8"))

    @staticmethod
    def committed_workflow():
        return yaml.safe_load((ROOT / WORKFLOW).read_text(encoding="utf-8"))

    def push(self, document, fault=None):
        self.serial += 1
        workdir = self.base / f"push-{self.serial}"
        log = workdir / "executed.jsonl"
        workdir.mkdir()
        log.touch()
        run = PushRun(document, self.origin, self.sha, workdir, repository=REPOSITORY, fault=fault,
                      extra_env={LOG_VARIABLE: str(log), "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}).run()
        run.executed = {}
        workspaces = {Path(record["workspace"]).resolve(): record["label"] for record in run.instances}
        for line in log.read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            label = workspaces.get(Path(row["cwd"]).resolve())
            node = f"{Path(row['file']).resolve().relative_to(Path(row['cwd']).resolve())}::{row['test']}"
            run.executed.setdefault(label, []).append(node)
        # Instances that measured at least one node, in run order.
        run.measuring = [record for record in run.instances if record["label"] in run.executed]
        return run

    def pr_restore_keys(self, document):
        """Keys the PR acceptance shards restore for a PR whose base is this push."""
        event = {"number": 1, "pull_request": {
            "number": 1, "base": {"sha": self.sha, "ref": "main"},
            "head": {"sha": "b" * 40, "ref": "foragent/issue-1", "repo": {"full_name": REPOSITORY}}},
            "repository": {"full_name": REPOSITORY, "default_branch": "main"}}
        github = {"event_name": "pull_request", "event": event, "sha": "c" * 40, "ref": "refs/pull/1/merge",
                  "ref_name": "1/merge", "repository": REPOSITORY, "base_ref": "main",
                  "head_ref": "foragent/issue-1", "workspace": "/w", "run_id": "1"}
        context = {"github": github, "runner": {"temp": "/t"}, "needs": {}, "env": {}, "vars": {},
                   "matrix": {"shard": 0}, "steps": {}}
        functions = {"success": lambda: True, "failure": lambda: False, "always": lambda: True,
                     "cancelled": lambda: False}
        keys = set()
        for job in document["jobs"].values():
            if not condition(job.get("if"), context, functions):
                continue
            for step in job.get("steps", []):
                if step.get("uses", "").split("@", 1)[0] in {"actions/cache/restore", "actions/cache"}:
                    keys.add(str(deep_render(step.get("with", {}).get("key", ""), context)))
        return keys

    def published_results(self, run, key):
        path = self.base / f"published-{self.serial}.json"
        files = run.caches[key]["files"]
        assert len(files) == 1, "The base cache is one file"
        path.write_bytes(files[0])
        return read_results(str(path), REPOSITORY, self.sha, self.scope)


def run_log(run):
    log = run.workdir / "run.log"
    return log.read_text(encoding="utf-8")[-6000:] if log.exists() else ""
