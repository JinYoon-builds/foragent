"""Shared offline helpers for unit tests."""

from copy import deepcopy
from dataclasses import asdict
from datetime import timedelta
from io import BytesIO
import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
import xml.etree.ElementTree as ET
from zipfile import ZipFile

import pytest

from conftest import ACCEPTANCE, git_command

from helpers.ci_runner_case import make_case
from helpers.ci_transport import REPO, RUN, RecordedCI, job
from helpers.home import offline_acceptance


_WORKFLOW = ".github/workflows/foragent-acceptance.yml"
_KEEP = "tests/acceptance/test_keep.py::test_keep"
_OTHER = "tests/acceptance/test_other.py::TestOther::test_other[value]"
_FUTURE = "tests/acceptance/test_future.py::test_future"
_LOCKED = "tests/acceptance/test_queued.py"
_EXCLUDED = sorted([ACCEPTANCE, _LOCKED])
_TEST_COMMAND = f"{shlex.quote(sys.executable)} -m pytest -q"
_CI_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures/github_ci"


def _github_fixture(name):
    # Keep GitHub's captured fields/nesting, rebasing repository identity onto
    # the offline history. Scenario-specific values are changed by ComparisonCI.
    recorded_repo = json.loads((_CI_FIXTURES / "repo.json").read_text())["full_name"]
    return json.loads((_CI_FIXTURES / name).read_text().replace(recorded_repo, REPO))


@pytest.fixture(autouse=True)
def ci_evidence_recording():
    """Replace conftest's legacy check/publisher enrichment in this module.

    The recording below supplies scope and trusted shard config directly. In
    particular, no fixture may inject default_branch into run.repository or
    turn the captured pull_request run into a workflow_run publisher.
    """


@pytest.fixture(autouse=True)
def no_external_execution(monkeypatch, offline_acceptance):
    from foragent import executors, scheduler

    def forbidden(*args, **kwargs):
        pytest.fail("ci-mode tests must not signal processes or start model turns")

    monkeypatch.setattr(os, "kill", forbidden)
    monkeypatch.setattr(os, "killpg", forbidden)
    monkeypatch.setattr(signal, "raise_signal", forbidden)
    monkeypatch.setattr(executors.ClaudeExecutor, "run", forbidden)
    monkeypatch.setattr(executors.CodexExecutor, "run", forbidden)
    monkeypatch.setattr(scheduler, "machine_load", lambda: ("normal", 0.1))
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")


def _select_mode(case, mode, fallback):
    # Exercise the behavior on old main too, rather than stopping every scenario
    # at an unknown Config constructor argument. Public loading is tested below.
    # These are ordinary proposed settings, supplied at the RunContext seam;
    # no baseline, comparison, selection or merge implementation is replaced.
    if mode is not None:
        object.__setattr__(case.config, "checks", mode)
    if fallback is not None:
        object.__setattr__(case.config, "checks_fallback", fallback)
    object.__setattr__(case.config, "ci_shards", 2)


class _CheckProcesses:
    """Observe real checks against tiny local suites, with no process timeout.

    The models and GitHub are fake. Running these few deterministic pytest
    functions makes marker/path selection observable without reimplementing
    pytest's selection rules. Additional JUnit is instrumentation only; the
    runner receives its real exit code and its requested report unchanged.
    """

    def __init__(self, case, monkeypatch):
        from foragent import runner

        self.case, self.calls = case, []
        self.unusable = False
        monkeypatch.setattr(runner, "run_logged", self.run)

    def run(self, argv, *, cwd, log_path, env=None, **kwargs):
        assert argv[:2] == ["/bin/sh", "-c"], argv
        words = shlex.split(argv[2])
        report = None
        for index, word in enumerate(words):
            if word.startswith(("--junitxml=", "--junit-xml=")):
                report = Path(word.split("=", 1)[1])
            elif word in {"--junitxml", "--junit-xml"}:
                report = Path(words[index + 1])
        is_pytest = "pytest" in words
        capture = is_pytest and Path(cwd) != self.case.store.worktree_path(1)
        call = SimpleNamespace(words=words, cwd=Path(cwd), capture=capture, executed=set(),
                               worker_calls=len(self.case.implementer.calls))
        self.calls.append(call)
        command = argv[2]
        if is_pytest and report is None:
            report = self.case.store.root / f"observed-{len(self.calls)}.xml"
            command += f" -o junit_family=legacy --junitxml={shlex.quote(str(report))}"
        if report is not None and not report.is_absolute():
            report = Path(cwd) / report
        result = subprocess.run(
            ["/bin/sh", "-c", command], cwd=cwd, capture_output=True, text=True,
            env={**os.environ, **(env or {})}, check=False,
        )
        log_path = Path(log_path)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as stream:
            stream.write(result.stdout + result.stderr)
        if report is not None and report.exists():
            for test in ET.parse(report).iter("testcase"):
                path, name = test.get("file"), test.get("name")
                if path and name:
                    module = path.removesuffix(".py").replace("/", ".")
                    classes = test.get("classname", "").removeprefix(module).lstrip(".")
                    call.executed.add("::".join(filter(None, [path, classes.replace(".", "::"), name])))
            if capture and self.unusable:
                report.write_text("not a JUnit report", encoding="utf-8")
        self.case.clock[0] += timedelta(seconds=2)
        return result.returncode, False, None

    @property
    def captures(self):
        return [call for call in self.calls if call.capture]

    @property
    def head_ids(self):
        return set().union(*(call.executed for call in self.calls if not call.capture))


class _ComparisonCI:
    """Real GhTracker reads; captured Actions artifacts; FakeTracker writes.

    Existing CI-not-started recordings supply jobs, annotations and logs. New
    pr_* readers use the same real GitHub command seam, so a fake comparison
    method cannot satisfy the test without parsing the recorded wire result.
    """

    def __init__(self, case, monkeypatch):
        from foragent.tracker import GhTracker

        self.case = case
        self.reads = []
        self.repo_reads = []
        self.artifact_reads = []
        self.downloads = []
        self.damage = None
        self.regression = None
        self.repaired = False
        self.never_started = False
        self.checks_only = False
        self.run_id = _github_fixture("measurement_run.json")["id"]
        self.legacy = RecordedCI(case, monkeypatch, [job(conclusion="success", annotation=None)])
        self.legacy.passing = True
        gh = GhTracker(REPO, self.command)
        fake = case.tracker

        class ReadThroughTracker:
            def __getattr__(self, name):
                if name.startswith("pr_") or not hasattr(fake, name):
                    return getattr(gh, name)
                return getattr(fake, name)

        case.ctx.tracker = ReadThroughTracker()

    def payload(self, index):
        nodes = [_KEEP, _FUTURE] if index == 0 else [_OTHER]
        passing = [_KEEP] if index == 0 else [_OTHER]
        head_nodes, head_passing = list(nodes), list(passing)
        if self.regression and not self.repaired and index == 0:
            head_passing.remove(_KEEP)
            if self.regression == "deselected":
                head_nodes.remove(_KEEP)
        payload = _github_fixture("shard-0.json")
        payload.update(base_sha=self.case.base_sha, head_sha=self.case.tracker.pr_head_sha(2),
                       shard_index=index, shard_count=self.case.config.ci_shards,
                       excluded=_EXCLUDED, duration_seconds=12.5 + index)
        payload["scope"].update(
            acceptance_dir=self.case.config.acceptance_dir,
            command=f"PYTEST_ADDOPTS= {shlex.quote(sys.executable)} -m pytest "
                    "-o addopts= --rootdir=. --maxfail=0 -p no:cacheprovider",
            excluded=_EXCLUDED, shard_count=self.case.config.ci_shards,
        )
        payload["base"].update(collected_ids=nodes, passing_ids=passing)
        payload["head"].update(collected_ids=head_nodes, passing_ids=head_passing)
        return payload

    def reports(self):
        payloads = [self.payload(0), self.payload(1)]
        damage = self.damage
        if damage == "missing":
            payloads = []
        elif damage == "partial":
            payloads.pop()
        elif damage == "duplicate":
            payloads[1] = deepcopy(payloads[0])
        elif damage == "stale-head":
            payloads[1]["head_sha"] = "a" * 40
        elif damage == "stale-base":
            payloads[1]["base_sha"] = "b" * 40
        elif damage == "foreign-repository":
            payloads[1]["repository"] = "owner/other"
        elif damage == "shard-count":
            payloads[1]["shard_count"] = 3
        elif damage == "incomplete-base":
            payloads[1]["base"]["complete"] = False
        elif damage == "incomplete-head":
            payloads[1]["head"]["complete"] = False
        elif damage == "missing-exclusions":
            payloads[1]["excluded"] = []
        elif damage == "extra-exclusions":
            for payload in payloads:
                payload["excluded"] = [*_EXCLUDED, _KEEP.split("::")[0]]
        elif damage == "unknown-schema":
            payloads[1]["schema_version"] = 99
        elif damage == "uncollected-pass":
            payloads[1]["head"]["collected_ids"] = []
        elif damage == "empty-shard":
            for side in ("base", "head"):
                payloads[1][side].update(collected_ids=[], passing_ids=[])
        elif damage == "foreign-scope":
            payloads[1]["scope"]["marker"] = "local_only"
        elif damage == "self-declared-shards":
            payloads.pop()
            payloads[0]["shard_count"] = payloads[0]["scope"]["shard_count"] = 1
        return payloads

    def measurement_run(self):
        run = _github_fixture("measurement_run.json")
        head = self.case.tracker.pr_head_sha(2)
        run["head_sha"] = run["head_commit"]["id"] = head
        if self.damage == "unfinished-run":
            run.update(status="in_progress", conclusion=None)
        elif self.damage == "failed-run":
            run["conclusion"] = "failure"
        elif self.damage == "foreign-workflow":
            run["path"] = ".github/workflows/other.yml"
        elif self.damage == "wrong-event":
            run["event"] = "workflow_run"
        elif self.damage == "stale-run-head":
            run["head_sha"] = "a" * 40
        return run

    def runs_by_head(self):
        response = _github_fixture("runs_by_head.json")
        ordinary = next(run for run in response["workflow_runs"] if run["path"] != _WORKFLOW)
        ordinary_id = ordinary["id"]
        ordinary.update(id=RUN, head_sha=self.case.tracker.pr_head_sha(2),
                        conclusion="success" if self.legacy.passing else "failure")
        for key in ("url", "html_url", "jobs_url", "logs_url", "artifacts_url"):
            ordinary[key] = ordinary[key].replace(str(ordinary_id), str(RUN))
        response["workflow_runs"] = [self.measurement_run(), ordinary]
        response["total_count"] = len(response["workflow_runs"])
        return response

    def artifacts(self):
        response = _github_fixture("artifacts.json")
        recorded = {artifact["name"]: artifact for artifact in response["artifacts"]}
        artifacts = []
        for index, payload in enumerate([] if self.checks_only else self.reports()):
            artifact = deepcopy(recorded[f"acceptance-{index}"])
            artifact["name"] = f"acceptance-{payload['shard_index']}"
            artifact["workflow_run"].update(id=self.run_id, head_sha=self.case.tracker.pr_head_sha(2))
            if self.damage == "foreign-run" and index == 1:
                artifact["workflow_run"]["id"] = self.run_id + 1
            if self.damage == "expired" and index == 1:
                artifact["expired"] = True
            artifacts.append(artifact)
        response.update(total_count=len(artifacts), artifacts=artifacts)
        return response

    def archive(self, artifact_id):
        for artifact, payload in zip(self.artifacts()["artifacts"], self.reports(), strict=True):
            if artifact["id"] != artifact_id:
                continue
            text = json.dumps(payload)
            if self.damage == "unparseable" and payload["shard_index"] == 1:
                text = "{truncated JSON"
            # Use the captured ZIP's member metadata and compression, replacing
            # only the report for this offline shard/scenario.
            with ZipFile(_CI_FIXTURES / "acceptance-0.zip") as captured:
                member = captured.getinfo("shard-0.json")
            member.filename = f"shard-{payload['shard_index']}.json"
            output = BytesIO()
            with ZipFile(output, "w") as archive:
                archive.writestr(member, text)
            return output.getvalue()
        pytest.fail(f"Unrecorded artifact download: {artifact_id}")

    def check_runs(self):
        # Only the check-only negative case exposes display reports. Their
        # suite really belongs to the pull_request measurement, not a publisher.
        if not self.checks_only:
            return []
        run = self.measurement_run()
        checks = []
        for index in range(self.case.config.ci_shards):
            checks.append({
                "id": 8100 + index, "name": f"foragent-acceptance/{index}",
                "head_sha": run["head_sha"], "status": "completed", "conclusion": "success",
                "app": {"slug": "github-actions"}, "check_suite": {"id": run["check_suite_id"]},
                "details_url": run["html_url"],
                "output": {"title": "Acceptance comparison", "summary": "Display only",
                           "text": json.dumps(self.payload(index))},
            })
        return checks

    def command(self, args, input=None):
        if args[:2] == ["pr", "checks"] and self.never_started:
            return "[]"
        if args[:1] == ["api"]:
            endpoint = next((arg for arg in args if arg.startswith(("repos/", "https://api.github.com/"))), "")
            parsed = urlsplit(endpoint)
            path = parsed.path.removeprefix("/")
            prefix = f"repos/{REPO}/"
            value = None
            if path == prefix.rstrip("/"):
                self.repo_reads.append(list(args))
                value = _github_fixture("repo.json")
            elif path == prefix + "actions/runs":
                query = parse_qs(parsed.query)
                value = self.runs_by_head()
                if "head_sha" in query:
                    self.reads.append((query["head_sha"][0], list(args)))
                    if query["head_sha"] != [self.case.tracker.pr_head_sha(2)]:
                        value.update(total_count=0, workflow_runs=[])
                elif "check_suite_id" in query:
                    # Serve GitHub's actual suite/run relationship even to the
                    # old reader, so it fails the feature rather than the fake.
                    value["workflow_runs"] = [run for run in value["workflow_runs"]
                                              if str(run["check_suite_id"]) == query["check_suite_id"][0]]
                    value["total_count"] = len(value["workflow_runs"])
            elif path == prefix + f"actions/runs/{self.run_id}":
                value = self.measurement_run()
            elif path == prefix + f"actions/runs/{self.run_id}/artifacts":
                self.artifact_reads.append(list(args))
                value = self.artifacts()
            elif path.startswith(prefix + "actions/artifacts/") and path.endswith("/zip"):
                artifact_id = int(path.split("/")[-2])
                self.downloads.append(artifact_id)
                return self.archive(artifact_id)  # Binary HTTP response, not JSON/text.
            if value is not None:
                if "--jq" in args:
                    projection = args[args.index("--jq") + 1]
                    value = value[projection.removeprefix(".")]
                return json.dumps(value)
            if path.startswith(prefix + "commits/") and path.endswith("/check-runs"):
                checks = self.check_runs()
                value = {"total_count": len(checks), "check_runs": checks}
                if "--jq" in args:
                    assert args[args.index("--jq") + 1] == ".check_runs", args
                    value = checks
                return json.dumps(value)
            if path.startswith(prefix + "check-runs/") and path.rsplit("/", 1)[-1].isdigit():
                number = int(path.rsplit("/", 1)[-1])
                return json.dumps(next(check for check in self.check_runs() if check["id"] == number))
            if path == prefix + "pulls/2/files":
                return json.dumps([{"filename": "src/demo.py"}])
        return self.legacy.command(args, input)


def mode_case(git_history, tmp_path, monkeypatch, *, mode="ci", fallback=None):
    from foragent.issue import body_hash

    git_history.update({
        "src/__init__.py": "", "src/demo.py": "VALUE = 1\n",
        "tests/test_demo.py": "from src.demo import VALUE\n\ndef test_demo():\n    assert VALUE > 0\n",
        "tests/test_importer.py": "from src.demo import VALUE\n\ndef test_importer():\n    assert VALUE > 0\n",
        _KEEP.split("::")[0]: (
            "from pathlib import Path\n\ndef test_keep():\n    assert not Path('broken-keep').exists()\n"
        ),
        _OTHER.split("::")[0]: (
            "import pytest\n\nclass TestOther:\n    @pytest.mark.parametrize('value', [1], ids=['value'])\n"
            "    def test_other(self, value):\n        assert value == 1\n"
        ),
        _FUTURE.split("::")[0]: "def test_future():\n    assert False, 'not implemented on base or head'\n",
        _LOCKED: "def test_queued():\n    assert False, 'owned by an open issue'\n",
        "tests/test_machine.py": (
            "from pathlib import Path\nimport pytest\n\n@pytest.mark.local_only\n"
            "def test_machine():\n    assert not Path('broken-machine').exists()\n\n"
            "def test_unmarked_neighbor():\n    assert True\n"
        ),
        "tests/acceptance/test_machine.py": (
            "import pytest\n\n@pytest.mark.local_only\ndef test_machine_acceptance():\n    assert True\n\n"
            "def test_portable_neighbor():\n    assert True\n"
        ),
        "pytest.ini": "[pytest]\nmarkers =\n    local_only: requires the user's machine\n",
        _WORKFLOW: (
            "name: foragent-acceptance\non: [pull_request]\njobs:\n  comparison:\n"
            "    runs-on: ubuntu-latest\n    steps:\n      - run: echo recorded-by-the-test\n"
        ),
    })
    git_command(git_history.clone, "fetch", "-q", "origin")
    git_command(git_history.clone, "merge", "-q", "--ff-only", "origin/main")
    case = make_case(git_history, tmp_path, monkeypatch, skill_libraries=[], test_command=_TEST_COMMAND,
                     max_retries=1, review_max=2)
    case.base_sha = git_history.tip
    _select_mode(case, mode, fallback)
    snapshot = asdict(case.store.load_approval(1))
    snapshot["body"] = snapshot["body"].replace(f"test -f {ACCEPTANCE}", f"{_TEST_COMMAND} {ACCEPTANCE}")
    snapshot["body_sha"] = body_hash(snapshot["body"])
    case.store.save_approval(**snapshot)
    case.tracker.edit_body(1, snapshot["body"])
    # Reserve the recorded PR number before adding another issue. Publication
    # still updates its real Git head through make_case's offline Git runner.
    assert case.tracker.create_pr(head="foragent/issue-1", base="main", title="Feature", body="WIP") == 2
    case.tracker.add_issue(9, "Queued feature", labels=["approved"])
    case.store.save_approval(9, body="Queued feature", body_sha="queued",
                             acceptance_blobs=case.git.blob_shas("origin/main", [_LOCKED]),
                             approved_by="human", approved_at=case.clock[0].isoformat())

    original_implement = case.implement

    def implement(kwargs):
        original_implement(kwargs)
        (kwargs["worktree"] / "src/demo.py").write_text("VALUE = 2\n")
        return "Implemented demo; own tests are tests/test_demo.py"

    case.implement = implement
    case.implementer.steps = [implement]
    case.processes = _CheckProcesses(case, monkeypatch)
    case.ci = _ComparisonCI(case, monkeypatch)
    return case


def run(case):
    from foragent.runner import run_issue

    return run_issue(1, case.ctx)
