"""Shared offline helpers for unit tests."""

from datetime import timedelta
import hashlib
import inspect
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tarfile
from threading import Event, RLock, local
import time
from types import SimpleNamespace
import xml.etree.ElementTree as ET

import pytest
import yaml

from conftest import BASE_TIME, git_command, git_history
from foragent.config import load_config
from foragent.executors import ExecResult
from foragent.git import GitRepo
from foragent.issue import body_hash
from foragent.runner import RunContext
from foragent.scheduler import TickContext, tick
from foragent.state import StateStore
from foragent.tracker import FakeTracker
from runner_support import import_index

from helpers.group_ci import GitHubCI, WORKFLOW


_ROOT = next(p for p in Path(__file__).resolve().parents if (p / "src/foragent").is_dir())
NUMBERS = (5, 1, 4, 2)
BLOCKER = 9000
CONTRACT = "tests/acceptance/test_contract.py"
_CLOSING_LOCK = "tests/acceptance/test_closing_owner.py"
_QUEUE = "merge-queue.jsonl"
_PYTHON = shlex.quote(sys.executable)


def rows(case):
    path = case.store.root / _QUEUE
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def ready(case, n):
    return [r for r in rows(case) if r["event"] == "ready" and r["n"] == n]


def turns(case):
    return [r["n"] for r in rows(case) if r["event"] == "turn" and r["result"] == "merged"]


def _archive(case, sha, destination):
    destination.mkdir(parents=True)
    stream = subprocess.run(["git", "archive", sha], cwd=case.history.origin,
                            check=True, capture_output=True).stdout
    with tarfile.open(fileobj=io.BytesIO(stream)) as bundle:
        bundle.extractall(destination, filter="data")


def _measured_suite(case, sha, excluded=()):
    """Actual pytest on the small repository, never the Foragent test suite."""
    key = sha, tuple(excluded)
    with case.measure_lock:
        if key in case.measurements:
            return case.measurements[key]
        directory = case.scratch / f"measurement-{len(case.measurements)}"
        _archive(case, sha, directory)
        xml = directory / "result.xml"
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
             "--rootdir=.", "-o", "addopts=", "-o", "junit_family=legacy", f"--junitxml={xml}",
             "tests/acceptance", *[f"--ignore={p}" for p in excluded]],
            cwd=directory, capture_output=True, text=True, timeout=15,
            env={**os.environ, "PYTEST_ADDOPTS": "", "PYTHONPATH": ""},
        )
        if result.returncode not in (0, 1, 5) or not xml.exists():
            raise RuntimeError(f"Small-repo pytest infrastructure failed: {result.stdout}\n{result.stderr}")
        collected, passing = [], []
        for node in ET.parse(xml).iter("testcase"):
            path = node.get("file") or node.get("classname").replace(".", "/") + ".py"
            identity = f"{path}::{node.get('name')}"
            collected.append(identity)
            if not any(node.find(tag) is not None for tag in ("failure", "error", "skipped")):
                passing.append(identity)
        suite = {"complete": True, "collected_ids": sorted(collected), "passing_ids": sorted(passing)}
        case.measurements[key] = suite
        return suite


class _MeasuredGitHubCI(GitHubCI):
    """Reuse GitHubCI's refs, PR boundary, artifact identity and immutable rounds."""

    def reports(self, row):
        templates = super().reports(row)
        trailer = git_command(self.case.history.origin, "log", "--first-parent",
                              "--format=%(trailers:key=Foragent-Acceptance-Excluded,valueonly)",
                              f"{row.base}..{row.head}")
        excluded = json.loads(next(line for line in trailer.splitlines() if line.strip()))
        base = _measured_suite(self.case, row.base, excluded)
        head = _measured_suite(self.case, row.head, excluded)
        for index, artifact in enumerate(templates):
            report = artifact["report"]
            report["excluded"] = excluded
            report["scope"]["excluded"] = excluded
            for side, suite in (("base", base), ("head", head)):
                # ci_pytest assigns by FILE hash, independently on both sides.
                # Collection-position slicing would invent losses on deletion.
                selected = [node for node in suite["collected_ids"] if int.from_bytes(
                    hashlib.sha256(node.split("::", 1)[0].encode()).digest(), "big") % 2 == index]
                report[side] = {"complete": True, "collected_ids": selected,
                                "passing_ids": [n for n in selected if n in suite["passing_ids"]]}
        return templates


class _GitHub(FakeTracker):
    """Offline GitHub service; merges update the actual bare origin's main tree."""

    def __init__(self, case):
        super().__init__(now=lambda: case.clock[0], strict=True)
        self.case = case

    def create_pr(self, **kwargs):
        with self.case.transport_lock:
            pr = super().create_pr(**kwargs)
            self.set_pr_checks(pr, "pass")
            head = git_command(self.case.history.origin, "rev-parse", kwargs["head"])
            paths = git_command(self.case.history.origin, "diff", "--name-only", "main", head).splitlines()
            self.set_pr_files(pr, paths)
            return pr

    def merge_pr(self, pr, *, expected_head_sha, **kwargs):
        case = self.case
        with case.transport_lock:
            before = git_command(case.history.origin, "rev-parse", "main")
            tree = git_command(case.history.origin, "merge-tree", "--write-tree", before,
                               expected_head_sha).splitlines()[0]
            # External service observes which loaded client code requested the merge.
            frames = [f for f in inspect.stack()
                      if Path(f.filename).resolve().is_relative_to(_ROOT / "src/foragent")]
            provenance = [{"path": f.filename, "function": f.function,
                           "sha256": hashlib.sha256(Path(f.filename).read_bytes()).hexdigest()}
                          for f in frames]
            super().merge_pr(pr, expected_head_sha=expected_head_sha, **kwargs)
            stream = ["commit refs/heads/main\n",
                      "committer Offline GitHub <tests@example.com> 1792483200 +0000\n",
                      "data 6\nSquash\n", f"from {before}\n", "deleteall\n"]
            for entry in git_command(case.history.origin, "ls-tree", "-r", "-z", tree).split("\0"):
                if entry:
                    metadata, name = entry.split("\t", 1)
                    mode, _, blob = metadata.split()
                    stream.append(f"M {mode} {blob} {json.dumps(name)}\n")
            git_command(case.history.origin, "fast-import", "--quiet", "--force",
                        input="".join(stream) + "\ndone\n")
            case.history.tip = git_command(case.history.origin, "rev-parse", "main")
            case.merges.append({"pr": pr, "head": expected_head_sha,
                                "main": case.history.tip, "client": provenance})


@pytest.fixture
def world(git_history, tmp_path, monkeypatch):
    """Own config, immutable CI artifacts and genuine waiting runner processes."""
    def build(*, scenario="ordinary", maximum=1, overlap=3):
        from helpers import group_ci as reusable

        monkeypatch.setattr(reusable, "MEMBERS", NUMBERS)
        case = SimpleNamespace(history=git_history, root=git_history.clone,
                               clock=[BASE_TIME + timedelta(hours=1)], worker=local(),
                               scratch=tmp_path / "measurements", transport_lock=RLock(),
                               measure_lock=RLock(), measurements={}, merges=[], armed=False,
                               released=Event(), scenario=scenario, outcomes={}, model_calls={})
        case.scratch.mkdir()
        settings = {"repo": "owner/repo", "review": "fast", "checks": "ci",
                    "base_branch": "main", "checks_fallback": None,
                    "executor": "claude", "reviewer": "codex", "approvers": ["human"],
                    "merge_group_max": maximum, "overlap_mode": "fixed", "overlap_per_file": overlap,
                    "test_slots": 4, "ci_shards": 2, "test_command": f"{_PYTHON} -m pytest -q",
                    "skill_libraries": [], "language": "en", "max_retries": 1}
        files = {".foragent.yml": yaml.safe_dump(settings), WORKFLOW:
                 "name: offline-ci\non: [pull_request]\njobs:\n  checks:\n    runs-on: ubuntu-latest\n    steps: []\n",
                 "src/value.py": "VALUE = 0\n",
                 CONTRACT: "import runpy\n\ndef test_contract():\n    assert runpy.run_path('src/value.py')['VALUE'] == 0\n",
                 _CLOSING_LOCK: "def test_closed_owner():\n    pass\n"}
        for n in NUMBERS:
            files[f"tests/acceptance/test_own_{n}.py"] = (
                "from pathlib import Path\n\ndef test_own():\n"
                f"    assert Path('src/member_{n}.py').is_file()\n")
        case.history.update(files)
        git_command(case.root, "fetch", "origin")
        git_command(case.root, "merge", "--ff-only", "origin/main")
        case.config = load_config(case.root, global_path=case.root / "absent-global.yml")
        case.base = case.history.tip
        case.store = StateStore(tmp_path / "state", now=lambda: case.clock[0])
        case.tracker = _GitHub(case)

        def transport(argv):
            with case.transport_lock:
                root, args = case.root, list(argv)
                while args[:1] == ["-C"]:
                    root, args = Path(args[1]), args[2:]
                if "commit" in args:
                    import_index(root, args[-1])
                    return ""
                if {"push", "reset", "checkout", "stash"}.intersection(args):
                    raise RuntimeError(f"Forbidden git operation escaped GitHubCI: {args}")
                return git_command(root, *args)

        case.git = GitRepo(case.root, transport)
        for n in (*NUMBERS, 77):
            path = _CLOSING_LOCK if n == 77 else f"tests/acceptance/test_own_{n}.py"
            body = ("<!-- foragent:v1 -->\n## Goal\nDeliver the independent member.\n"
                    "## Out of scope\nOther members.\n## Files\n"
                    f"- src/member_{n}.py\n## Done when\n```sh\n"
                    f"{_PYTHON} -m pytest -q -p no:cacheprovider {path}\n```\n")
            case.tracker.add_issue(n, f"Member {n}", body)
            case.tracker.record_label(n, "approved", "human")
            case.store.save_approval(n, body=body, body_sha=body_hash(body),
                                    acceptance_blobs=case.git.blob_shas("origin/main", [path]),
                                    approved_by="human", approved_at=case.clock[0].isoformat())
        # A previously merged owner makes CONTRACT a passing locked file.
        body = f"## Done when\n```sh\n{_PYTHON} -m pytest -q {CONTRACT}\n```\n"
        case.tracker.add_issue(78, "Existing completed contract", body, state="closed")
        case.store.save_approval(78, body=body, body_sha=body_hash(body),
                                acceptance_blobs=case.git.blob_shas("origin/main", [CONTRACT]),
                                approved_by="human", approved_at=case.clock[0].isoformat())
        case.store.start_run(78, branch="previous-contract", worktree=case.store.root / "previous-contract")
        case.store.finish_run(78, outcome="merged")
        case.tracker.add_issue(BLOCKER, "Existing merge turn", state="closed")

        class Model:
            def __init__(self, review):
                self.review = review

            def run(self, **kwargs):
                if self.review:
                    return ExecResult(0, False, "VERDICT: PASS", kwargs["log_path"])
                n = case.worker.number
                case.model_calls[n] = case.model_calls.get(n, 0) + 1
                if case.model_calls[n] > 1:
                    return ExecResult(1, False, "Rework received; implementation still needs repair", kwargs["log_path"])
                root = kwargs["worktree"]
                (root / f"src/member_{n}.py").write_text(f"VALUE = {n}\n")
                if n == NUMBERS[0] and scenario == "lost":
                    (root / "src/value.py").write_text("VALUE = 1\n")
                return ExecResult(0, False, "Implemented", kwargs["log_path"])

        models = {"claude": Model(False), "codex": Model(True)}

        def sleep(seconds):
            # Scheduling yield only; wall time does not consume the fake work budget.
            case.released.wait(0.01)
            if case.released.is_set():
                time.sleep(0.01)

        case.ctx = RunContext("owner/repo", case.root, case.tracker, case.store,
                              case.config, case.git, now=lambda: case.clock[0],
                              executor_factory=models.__getitem__, sleep=sleep,
                              monotonic=lambda: case.clock[0].timestamp(),
                              poll_hook=lambda execute, poll: execute())
        case.ci = _MeasuredGitHubCI(case, monkeypatch, "passed")
        # The real tick refreshes the issue snapshot and validates approvals.
        tick(TickContext("owner/repo", case.root, case.tracker, case.store,
                         case.config, case.git, now=lambda: case.clock[0],
                         launcher=lambda n: None, start_runs=False))
        for n in NUMBERS:
            case.store.start_run(n, branch=f"foragent/issue-{n}", worktree=case.store.worktree_path(n))
            case.tracker.add_labels(n, ["running"])
        # Initial state is an existing owner's turn. Only the four real runners
        # create the tested PRs and readiness records. No queue function is faked.
        blocker = case.store.start_run(BLOCKER, branch="existing", worktree=case.store.root / "existing")
        (case.store.root / _QUEUE).write_text(json.dumps({
            "event": "ready", "n": BLOCKER, "started_at": blocker.started_at,
            "target": "main", "head_sha": case.base, "at": case.clock[0].isoformat(),
        }) + "\n")
        return case
    return build
