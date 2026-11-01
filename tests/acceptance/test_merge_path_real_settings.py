"""Locked I1 merge-path-pin contract; do not edit during implementation.

Run only this file. Real runner, scheduler tick, settings loader, Git, state,
pytest and watch rendering execute. GitHubCI is the existing external transport;
its reports below contain real pytest results on immutable base/head archives.
The model and clock are supplied through the public contexts. No merge, queue,
comparison, overlap, check or state function is monkeypatched here.

The repository conftest supplies its normal machine/HOME isolation. A Git adapter
imports staged snapshots instead of executing the run's forbidden git commit.
GitHubCI transfers remote refs by fetch instead of executing git push.
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
import hashlib
from importlib import metadata
import importlib.util
import inspect
import io
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tarfile
import tomllib
from threading import Event, RLock, local
import time
from types import SimpleNamespace
import xml.etree.ElementTree as ET
import zipfile

import pytest
import yaml

# Also runnable at its draft location, without copying files into tests/.
ROOT = next(p for p in Path(__file__).resolve().parents if (p / "src/foragent").is_dir())
for directory in (ROOT / "tests", ROOT / "tests/acceptance"):
    if str(directory) not in sys.path:
        sys.path.append(str(directory))

# Normal publication discovers tests/conftest.py automatically. At the draft
# path explicitly load the same isolation hooks, so the direct command works.
pytest_plugins = [] if Path(__file__).resolve().is_relative_to(ROOT / "tests") else ["conftest"]

from conftest import BASE_TIME, git_command, git_history  # noqa: E402, F401
from runner_support import import_index  # noqa: E402
from test_ci_group_evidence import GitHubCI, WORKFLOW  # noqa: E402
from foragent.config import load_config  # noqa: E402
from foragent.executors import ExecResult  # noqa: E402
from foragent.git import GitRepo  # noqa: E402
from foragent.issue import body_hash  # noqa: E402
from foragent.runner import RunContext, run_issue  # noqa: E402
from foragent.scheduler import TickContext, tick  # noqa: E402
from foragent.state import StateStore  # noqa: E402
from foragent.tracker import FakeTracker  # noqa: E402

_policy_path = Path(__file__).resolve().parents[1] / "merge_path_expected_red.py"
_policy_spec = importlib.util.spec_from_file_location("merge_path_expected_red", _policy_path)
_policy = importlib.util.module_from_spec(_policy_spec)
_policy_spec.loader.exec_module(_policy)
expected_red = _policy.expected_red

NUMBERS = (5, 1, 4, 2)
BLOCKER = 9000
CONTRACT = "tests/acceptance/test_contract.py"
CLOSING_LOCK = "tests/acceptance/test_closing_owner.py"
QUEUE = "merge-queue.jsonl"
PYTHON = shlex.quote(sys.executable)


def rows(case):
    path = case.store.root / QUEUE
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def ready(case, n):
    return [r for r in rows(case) if r["event"] == "ready" and r["n"] == n]


def turns(case):
    return [r["n"] for r in rows(case) if r["event"] == "turn" and r["result"] == "merged"]


def archive(case, sha, destination):
    destination.mkdir(parents=True)
    stream = subprocess.run(["git", "archive", sha], cwd=case.history.origin,
                            check=True, capture_output=True).stdout
    with tarfile.open(fileobj=io.BytesIO(stream)) as bundle:
        bundle.extractall(destination, filter="data")


def measured_suite(case, sha, excluded=()):
    """Actual pytest on the small repository, never the Foragent test suite."""
    key = sha, tuple(excluded)
    with case.measure_lock:
        if key in case.measurements:
            return case.measurements[key]
        directory = case.scratch / f"measurement-{len(case.measurements)}"
        archive(case, sha, directory)
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


class MeasuredGitHubCI(GitHubCI):
    """Reuse GitHubCI's refs, PR boundary, artifact identity and immutable rounds."""

    def reports(self, row):
        templates = super().reports(row)
        trailer = git_command(self.case.history.origin, "log", "--first-parent",
                              "--format=%(trailers:key=Foragent-Acceptance-Excluded,valueonly)",
                              f"{row.base}..{row.head}")
        excluded = json.loads(next(line for line in trailer.splitlines() if line.strip()))
        base = measured_suite(self.case, row.base, excluded)
        head = measured_suite(self.case, row.head, excluded)
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


class GitHub(FakeTracker):
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
                      if Path(f.filename).resolve().is_relative_to(ROOT / "src/foragent")]
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
        import test_ci_group_evidence as reusable

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
                    "test_slots": 4, "ci_shards": 2, "test_command": f"{PYTHON} -m pytest -q",
                    "skill_libraries": [], "language": "en", "max_retries": 1}
        files = {".foragent.yml": yaml.safe_dump(settings), WORKFLOW:
                 "name: offline-ci\non: [pull_request]\njobs:\n  checks:\n    runs-on: ubuntu-latest\n    steps: []\n",
                 "src/value.py": "VALUE = 0\n",
                 CONTRACT: "import runpy\n\ndef test_contract():\n    assert runpy.run_path('src/value.py')['VALUE'] == 0\n",
                 CLOSING_LOCK: "def test_closed_owner():\n    pass\n"}
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
        case.tracker = GitHub(case)

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
            path = CLOSING_LOCK if n == 77 else f"tests/acceptance/test_own_{n}.py"
            body = ("<!-- foragent:v1 -->\n## Goal\nDeliver the independent member.\n"
                    "## Out of scope\nOther members.\n## Files\n"
                    f"- src/member_{n}.py\n## Done when\n```sh\n"
                    f"{PYTHON} -m pytest -q -p no:cacheprovider {path}\n```\n")
            case.tracker.add_issue(n, f"Member {n}", body)
            case.tracker.record_label(n, "approved", "human")
            case.store.save_approval(n, body=body, body_sha=body_hash(body),
                                    acceptance_blobs=case.git.blob_shas("origin/main", [path]),
                                    approved_by="human", approved_at=case.clock[0].isoformat())
        # A previously merged owner makes CONTRACT a passing locked file.
        body = f"## Done when\n```sh\n{PYTHON} -m pytest -q {CONTRACT}\n```\n"
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
        case.ci = MeasuredGitHubCI(case, monkeypatch, "passed")
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
        (case.store.root / QUEUE).write_text(json.dumps({
            "event": "ready", "n": BLOCKER, "started_at": blocker.started_at,
            "target": "main", "head_sha": case.base, "at": case.clock[0].isoformat(),
        }) + "\n")
        return case
    return build


def exercise(case, waiting=lambda case: None):
    """Four approved PRs go through runner -> CI -> queue -> actual main merge."""
    def run(n):
        case.worker.number = n
        return run_issue(n, case.ctx)

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {}
        try:
            for n in NUMBERS:
                futures[n] = pool.submit(run, n)
                limit = time.monotonic() + 30
                while not ready(case, n) and not futures[n].done() and time.monotonic() < limit:
                    time.sleep(0.01)
                if not ready(case, n) and not (case.scenario == "lost"
                                              and n == NUMBERS[0] and futures[n].done()):
                    raise RuntimeError(f"Runner {n} failed before queue: " + case.store.log_path(n).read_text())
            case.waiting_queue = (case.store.root / QUEUE).read_bytes()
            waiting(case)
            case.armed = True
        finally:
            case.store.finish_run(BLOCKER, outcome="merged")
            case.released.set()
        case.outcomes = {n: future.result(timeout=60) for n, future in futures.items()}
    print("MERGE_PATH", json.dumps({"scenario": case.scenario, "outcomes": case.outcomes,
                                    "turns": turns(case), "ci_rounds": len(case.ci.by_head)}))


def assert_merged(case, expected=NUMBERS):
    actual = [int(next(p.head for p in case.tracker.created_prs if p.number == pr).rsplit("-", 1)[1])
              for pr in case.tracker.merged_prs]
    assert actual == list(expected), f"Wrong PR merge order: {actual}; {case.outcomes}"
    assert turns(case) == list(expected), "Queue history must match the actual merged PRs exactly"
    for n in expected:
        assert case.tracker.get_issue(n).state == "closed"
        run = case.store.get_run(n)
        assert (n, run.started_at) in case.store.merge_times() and run.outcome == "merged", "Persist the runner's successful merge"
        assert git_command(case.history.origin, "show", f"main:src/member_{n}.py") == f"VALUE = {n}"
    assert (case.store.root / QUEUE).read_bytes().startswith(case.waiting_queue)


def assert_rework(case, n):
    issue = case.tracker.get_issue(n)
    assert issue.state == "open" and issue.labels & {"blocked", "ready"}, "Return the lost-comparison PR to rework"
    pr = next(p for p in case.tracker.created_prs if p.head == f"foragent/issue-{n}")
    assert pr.merged_at is None and n not in turns(case), "A regression must never merge"
    assert f"src/member_{n}.py" not in git_command(case.history.origin, "ls-tree", "-r", "--name-only", "main").splitlines()
    comments = "\n".join(c.body for c in case.tracker.list_comments(n))
    assert "Acceptance baseline regression" in comments, "Rework must retain the failing comparison's reason"


def test_a_priority_changes_while_waiting(world):
    """Reject keeping stale FIFO priority after the waiting PRs are reprioritized."""
    case = world()
    def change(case):
        from foragent import orch_queue
        # Keep the existing owner ahead until the external event releases it.
        orch_queue.set_global(case.store, [BLOCKER, 2, 4, 1, 5])
    exercise(case, change)
    assert_merged(case, (2, 4, 1, 5))


def test_b_main_moves_while_waiting(world):
    """Reject overwriting a main commit that arrived after all ready heads were green."""
    case = world()
    exercise(case, lambda c: c.history.update({"src/neighbor.py": "VALUE = 'new main'\n"}))
    assert_merged(case)
    assert git_command(case.history.origin, "show", "main:src/neighbor.py") == "VALUE = 'new main'"


def test_c_new_main_contract_rejects_green_head(world):
    """Reject #1062: merging a green head whose combination fails main's new contract."""
    case = world()
    path = "tests/acceptance/test_new_main_contract.py"
    contract = ("from pathlib import Path\n\ndef test_new_contract():\n"
                f"    assert not Path('src/member_{NUMBERS[0]}.py').exists()\n")
    exercise(case, lambda c: c.history.update({path: contract}))
    assert_merged(case, NUMBERS[1:])
    assert_rework(case, NUMBERS[0])
    main = measured_suite(case, case.history.tip, [f"tests/acceptance/test_own_{n}.py" for n in (NUMBERS[0],)])
    assert main["collected_ids"] and main["passing_ids"] == main["collected_ids"], "Main must stay green after refusing the incompatible PR"


@expected_red("test_d_lost_comparison_never_joins_queue")
def test_d_lost_comparison_never_joins_queue(world):
    """Reject admitting a CI-green PR to the merge queue before discovering its lost passing test."""
    case = world(scenario="lost")
    exercise(case)
    assert_merged(case, NUMBERS[1:])
    assert_rework(case, NUMBERS[0])
    assert ready(case, NUMBERS[0]) == [], "Lost comparison must go to rework BEFORE any ready entry"


@expected_red("test_e_closed_owner_keeps_waiting_evidence_valid")
def test_e_closed_owner_keeps_waiting_evidence_valid(world):
    """Reject discarding immutable CI evidence solely because its excluded test owner closed."""
    case = world()
    def close(case):
        # The owner finishes its merge while the PRs wait. Closed-without-merge
        # still owns its locks on this base (#327), so include the real ledger.
        case.store.start_run(77, branch="closing-owner", worktree=case.store.root / "closing-owner")
        case.store.finish_run(77, outcome="merged")
        case.tracker.close_issue(77)
        case.closed_heads = {n: case.tracker.pr_head_sha(case.tracker.find_pr(f"foragent/issue-{n}"))
                             for n in NUMBERS}
    exercise(case, close)
    assert_merged(case)
    for n in NUMBERS[:1]:
        # FakeTracker.find_pr only returns open PRs; inspect immutable PR state.
        pr = next(p.number for p in case.tracker.created_prs if p.head == f"foragent/issue-{n}")
        assert case.tracker.pr_head_sha(pr) == case.closed_heads[n], "Closure alone cannot require a new published head"


def install_new_tool(case):
    """A real offline uv installation of a wheel containing the current package."""
    wheel = case.scratch / "foragent-9999.0-py3-none-any.whl"
    info = "foragent-9999.0.dist-info"
    with zipfile.ZipFile(wheel, "w") as output:
        for path in (ROOT / "src/foragent").rglob("*.py"):
            content = path.read_bytes()
            output.writestr(path.relative_to(ROOT / "src").as_posix(), content)
        requirements = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["dependencies"]
        output.writestr(f"{info}/METADATA", "Metadata-Version: 2.1\nName: foragent\nVersion: 9999.0\n"
                        + "".join(f"Requires-Dist: {requirement}\n" for requirement in requirements))
        output.writestr(f"{info}/WHEEL", "Wheel-Version: 1.0\nGenerator: merge-path\nRoot-Is-Purelib: true\nTag: py3-none-any\n")
        output.writestr(f"{info}/entry_points.txt", "[console_scripts]\nforagent = foragent.cli:main\n")
        output.writestr(f"{info}/RECORD", "")
    # Repackage the already installed dependency distributions into the local
    # wheelhouse. The new tool is runnable, with no network or fake installer.
    for dependency in ("PyYAML", "psutil"):
        distribution = metadata.distribution(dependency)
        tag = next(line.removeprefix("Tag: ") for line in distribution.read_text("WHEEL").splitlines()
                   if line.startswith("Tag: "))
        name = distribution.metadata["Name"].replace("-", "_")
        dependency_wheel = case.scratch / f"{name}-{distribution.version}-{tag}.whl"
        with zipfile.ZipFile(dependency_wheel, "w") as output:
            for path in distribution.files:
                source = distribution.locate_file(path)
                if ".." not in path.parts and source.is_file():
                    output.write(source, str(path))
    tool_dir = case.scratch / "tools"
    environment = {**os.environ, "UV_TOOL_DIR": str(tool_dir),
                   "UV_TOOL_BIN_DIR": str(case.scratch / "bin"), "UV_CACHE_DIR": str(case.scratch / "uv-cache"),
                   "UV_PYTHON_DOWNLOADS": "never"}
    installed = subprocess.run([shutil.which("uv"), "tool", "install", "--offline",
                               "--find-links", str(case.scratch), "--python",
                               sys.executable, str(wheel)], env=environment,
                              capture_output=True, text=True, timeout=30)
    if installed.returncode:
        raise RuntimeError(f"Offline tool install failed: {installed.stderr}")
    python = tool_dir / "foragent/bin/python"
    result = subprocess.run([str(python), "-I", "-c",
                             "import foragent; from importlib.metadata import version; "
                             "print(foragent.__file__); print(version('foragent'))"],
                            check=True, capture_output=True, text=True)
    case.installed = result.stdout.strip().splitlines()
    subprocess.run([str(python), "-I", "-m", "foragent.cli", "--help"], check=True,
                   capture_output=True, text=True, timeout=15)


def test_f_install_during_wait_records_merging_code(world, record_property):
    """Reject losing merge attribution when an isolated tool installation changes during a wait."""
    case = world()
    exercise(case, install_new_tool)
    assert_merged(case)
    record = {"new_install": case.installed, "merge_clients": [m["client"] for m in case.merges]}
    record_property("merge_code_provenance", json.dumps(record))
    print("MERGING_CODE", json.dumps(record))
    assert case.merges, "Observe actual merges before checking their callers"
    for merge in case.merges:
        assert any(Path(frame["path"]).resolve() != ROOT / "src/foragent/tracker.py"
                   for frame in merge["client"]), "Record a production caller outside the tracker wrapper for every merge"


@expected_red("test_g_twenty_shared_file_runs_watch_once_has_bounded_work")
def test_g_twenty_shared_file_runs_watch_once_has_bounded_work(world, capsys, record_property):
    """Reject a watch frame spending unbounded overlap work on twenty same-file waiting runs."""
    from foragent import status, watch
    case = world(overlap=20)
    # The file limit permits the twenty active owners; it is set in this test's
    # own config, independently of the repository's default of three.
    # Persist twenty real run records with overlapping approved claims. No fake
    # view/status/overlap helper is involved. Mixed globs retain the common file.
    for n in range(101, 121):
        body = ("## Goal\nShared file work.\n## Files\n- src/shared.py\n- src/*.py\n"
                f"## Done when\n```sh\n{PYTHON} -m pytest -q {CONTRACT}\n```\n")
        case.tracker.add_issue(n, f"Shared member {n}", body, labels=["approved", "running"])
        case.store.record_issue(case.tracker.get_issue(n), {})
        case.store.save_approval(n, body=body, body_sha=body_hash(body), acceptance_blobs={},
                                approved_by="human", approved_at=case.clock[0].isoformat())
        case.store.start_run(n, branch=f"foragent/issue-{n}", worktree=case.store.worktree_path(n))
        case.store.update_run(n, phase="merge-lock-wait")
    # Deterministic Python work ceiling, instead of a flaky 10s assertion.
    # Count every Foragent module, so moving the computation cannot evade the
    # bound. 200,000 source-line events permit thousands of comparisons.
    work = 0
    source_prefix = str(ROOT / "src/foragent") + os.sep
    class WorkLimit(BaseException):
        """Leave optional view collectors too; they may catch Exception."""

    def count(frame, event, arg):
        nonlocal work
        if event == "line" and frame.f_code.co_filename.startswith(source_prefix):
            work += 1
            if work > 200_000:
                raise WorkLimit()
        return count
    started = time.monotonic()
    exhausted = False
    sys.settrace(count)
    try:
        watch.run(lambda: status.collect(case.store, case.config, case.clock[0]),
                  case.tracker, case.store, case.config, case.root, once=True, now=lambda: case.clock[0])
    except WorkLimit:
        exhausted = True
    finally:
        sys.settrace(None)
    elapsed = time.monotonic() - started
    record_property("watch_seconds", elapsed)
    record_property("foragent_line_events", work)
    assert not exhausted and work <= 200_000, "watch --once must complete within the deterministic work budget"
    assert "Shared member" in capsys.readouterr().out, "watch --once must actually render the waiting runs"


@expected_red("test_h_one_ci_round_covers_multiple_member_merges")
def test_h_one_ci_round_covers_multiple_member_merges(world):
    """Reject new member CI rounds after one four-member combined CI pass."""
    case = world(maximum=4)
    exercise(case)
    assert_merged(case)
    group_prs = [pr for pr in case.tracker.created_prs if pr.head.startswith("foragent/group-")]
    assert len(group_prs) == 1, "Exactly one combined CI PR must cover this ready batch"
    assert case.tracker.get_issue(group_prs[0].number).state == "closed", "Clean up the measured group PR"
    # Published PR heads and queue heads are observable PR/queue state. A
    # group pass should leave at least two original ready PR heads unchanged.
    reused = [n for n in NUMBERS if next(m["head"] for m in case.merges
               if m["pr"] == next(p.number for p in case.tracker.created_prs
                                 if p.head == f"foragent/issue-{n}")) == ready(case, n)[0]["head_sha"]]
    assert len(reused) >= 2, f"Only {reused} merged their original CI-ready head; one round must cover >=2"
