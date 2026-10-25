"""Acceptance test for ci-mode. Locked by Foragent; do not edit."""

from copy import deepcopy
from dataclasses import asdict
from datetime import timedelta
import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
from types import SimpleNamespace
from urllib.parse import urlsplit
import xml.etree.ElementTree as ET

import pytest
import yaml

from conftest import ACCEPTANCE, git_command
from test_ci_feedback import make_case
from test_ci_not_started import ANNOTATION, REPO, RUN, RecordedCI, job
from test_local_truth import record_gh
from test_orch_review_handoff import bind_cli, tick_context
from test_skills_autosync import cli_exit, isolated_home
from test_state_split import offline_acceptance


# Wire contract chosen for this feature (no new production API is imported):
# GET repos/{repo}/commits/{PR_HEAD}/check-runs, including every page, returns
# completed check runs named foragent-acceptance/{zero-based shard index}.
# output.text is JSON with the shape returned by ComparisonCI.payload below.
# All shard_count distinct indices must report complete base AND head suites
# for the same repository, base/head SHAs and exact locked-file exclusions.
# A completed/success check means the measurement finished; compare passing_ids
# independently of the ordinary CI bucket. An empty complete shard is valid.
# Generated jobs expose this JSON as outputs.comparison and publish check runs.
# Run history and `fa orch facts --json` expose
# comparison = {source: "local" | "ci", duration_seconds: nonnegative number}.
# Machine-dependent tests use pytest.mark.local_only. CI excludes this marker;
# the local runner selects it across the project, plus the worker's own tests.

WORKFLOW = ".github/workflows/foragent-acceptance.yml"
KEEP = "tests/acceptance/test_keep.py::test_keep"
OTHER = "tests/acceptance/test_other.py::TestOther::test_other[value]"
FUTURE = "tests/acceptance/test_future.py::test_future"
LOCKED = "tests/acceptance/test_queued.py"
LOCAL = "tests/test_machine.py::test_machine"
LOCAL_ACCEPTANCE = "tests/acceptance/test_machine.py::test_machine_acceptance"
OWN = "tests/test_demo.py::test_demo"
OWN_ACCEPTANCE = f"{ACCEPTANCE}::test_feature"
EXCLUDED = sorted([ACCEPTANCE, LOCKED])
TEST_COMMAND = f"{shlex.quote(sys.executable)} -m pytest -q"


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


def select_mode(case, mode, fallback):
    # Exercise the behavior on old main too, rather than stopping every scenario
    # at an unknown Config constructor argument. Public loading is tested below.
    # These are ordinary proposed settings, supplied at the RunContext seam;
    # no baseline, comparison, selection or merge implementation is replaced.
    if mode is not None:
        object.__setattr__(case.config, "checks", mode)
    if fallback is not None:
        object.__setattr__(case.config, "checks_fallback", fallback)


class CheckProcesses:
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


class ComparisonCI:
    """Real GhTracker reads; recorded check-run output; FakeTracker writes.

    Existing CI-not-started recordings supply jobs, annotations and logs. New
    pr_* readers use the same real GitHub command seam, so a fake comparison
    method cannot satisfy the test without parsing the recorded wire result.
    """

    def __init__(self, case, monkeypatch):
        from foragent.tracker import GhTracker

        self.case = case
        self.reads = []
        self.damage = None
        self.regression = None
        self.repaired = False
        self.never_started = False
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
        nodes = [KEEP, FUTURE] if index == 0 else [OTHER]
        passing = [KEEP] if index == 0 else [OTHER]
        head_nodes, head_passing = list(nodes), list(passing)
        if self.regression and not self.repaired and index == 0:
            head_passing.remove(KEEP)
            if self.regression == "deselected":
                head_nodes.remove(KEEP)
        return {
            "schema_version": 1, "repository": REPO,
            "base_sha": self.case.base_sha, "head_sha": self.case.tracker.pr_head_sha(2),
            "shard_index": index, "shard_count": 2, "excluded": EXCLUDED,
            "duration_seconds": 12.5 + index,
            "base": {"complete": True, "collected_ids": nodes, "passing_ids": passing},
            "head": {"complete": True, "collected_ids": head_nodes, "passing_ids": head_passing},
        }

    def check_runs(self):
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
                payload["excluded"] = [*EXCLUDED, KEEP.split("::")[0]]
        elif damage == "unknown-schema":
            payloads[1]["schema_version"] = 99
        elif damage == "uncollected-pass":
            payloads[1]["head"]["collected_ids"] = []
        elif damage == "empty-shard":
            for side in ("base", "head"):
                payloads[1][side].update(collected_ids=[], passing_ids=[])
        checks = []
        for index, payload in enumerate(payloads):
            text = json.dumps(payload)
            if damage == "unparseable" and index == 1:
                text = "{truncated JSON"
            checks.append({
                "id": 8100 + index, "name": f"foragent-acceptance/{payload['shard_index']}",
                # The check belongs to this commit; its body can accidentally
                # contain a report copied from an older head.
                "head_sha": self.case.tracker.pr_head_sha(2), "status": "completed", "conclusion": "success",
                "details_url": f"https://github.com/{REPO}/actions/runs/{RUN}",
                "output": {"title": "Acceptance comparison", "summary": "Shard measurement", "text": text},
            })
        if damage == "unfinished-check":
            checks[1].update(status="in_progress", conclusion=None)
        return checks

    def command(self, args, input=None):
        if args[:2] == ["pr", "checks"] and self.never_started:
            return "[]"
        if args[:1] == ["api"]:
            endpoint = next((arg for arg in args if arg.startswith(("repos/", "https://api.github.com/"))), "")
            path = urlsplit(endpoint).path.removeprefix("/")
            prefix = f"repos/{REPO}/"
            if path.startswith(prefix + "commits/") and path.endswith("/check-runs"):
                self.reads.append((self.case.tracker.pr_head_sha(2), list(args)))
                checks = self.check_runs()
                value = {"total_count": len(self.check_runs()), "check_runs": checks}
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
        KEEP.split("::")[0]: (
            "from pathlib import Path\n\ndef test_keep():\n    assert not Path('broken-keep').exists()\n"
        ),
        OTHER.split("::")[0]: (
            "import pytest\n\nclass TestOther:\n    @pytest.mark.parametrize('value', [1], ids=['value'])\n"
            "    def test_other(self, value):\n        assert value == 1\n"
        ),
        FUTURE.split("::")[0]: "def test_future():\n    assert False, 'not implemented on base or head'\n",
        LOCKED: "def test_queued():\n    assert False, 'owned by an open issue'\n",
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
        WORKFLOW: (
            "name: foragent-acceptance\non: [pull_request]\njobs:\n  comparison:\n"
            "    runs-on: ubuntu-latest\n    steps:\n      - run: echo recorded-by-the-test\n"
        ),
    })
    git_command(git_history.clone, "fetch", "-q", "origin")
    git_command(git_history.clone, "merge", "-q", "--ff-only", "origin/main")
    case = make_case(git_history, tmp_path, monkeypatch, skill_libraries=[], test_command=TEST_COMMAND,
                     max_retries=1, review_max=2)
    case.base_sha = git_history.tip
    select_mode(case, mode, fallback)
    snapshot = asdict(case.store.load_approval(1))
    snapshot["body"] = snapshot["body"].replace(f"test -f {ACCEPTANCE}", f"{TEST_COMMAND} {ACCEPTANCE}")
    snapshot["body_sha"] = body_hash(snapshot["body"])
    case.store.save_approval(**snapshot)
    case.tracker.edit_body(1, snapshot["body"])
    # Reserve the recorded PR number before adding another issue. Publication
    # still updates its real Git head through make_case's offline Git runner.
    assert case.tracker.create_pr(head="foragent/issue-1", base="main", title="Feature", body="WIP") == 2
    case.tracker.add_issue(9, "Queued feature", labels=["approved"])
    case.store.save_approval(9, body="Queued feature", body_sha="queued",
                             acceptance_blobs=case.git.blob_shas("origin/main", [LOCKED]),
                             approved_by="human", approved_at=case.clock[0].isoformat())

    original_implement = case.implement

    def implement(kwargs):
        original_implement(kwargs)
        (kwargs["worktree"] / "src/demo.py").write_text("VALUE = 2\n")
        return "Implemented demo; own tests are tests/test_demo.py"

    case.implement = implement
    case.implementer.steps = [implement]
    case.processes = CheckProcesses(case, monkeypatch)
    case.ci = ComparisonCI(case, monkeypatch)
    return case


def run(case):
    from foragent.runner import run_issue

    return run_issue(1, case.ctx)


def assert_remote_only(case):
    assert not case.processes.captures, "checks: ci still captured an acceptance baseline on this machine"
    assert not ({KEEP, OTHER, FUTURE} & case.processes.head_ids), (
        "checks: ci must read CI's passing-set comparison instead of verifying it locally"
    )
    assert not list((case.store.root.parent / "baselines").glob("*.json")), "CI populated the local baseline cache"


def comparison_record(case, source):
    path = case.store.root / "runs.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    row = next(row for row in reversed(rows) if row["number"] == 1)
    comparison = row.get("comparison")
    assert isinstance(comparison, dict), "Every completed merge must persist its comparison source and duration"
    assert comparison.get("source") == source
    duration = comparison.get("duration_seconds")
    assert type(duration) in (int, float) and duration > 0, "Persist a measured comparison duration"
    if source == "ci":
        assert duration >= 13.5, "Record CI execution time from shard reports, not the local HTTP fetch duration"
    return comparison


def assert_infrastructure_hold(case, outcome):
    from foragent.reasons import GROUP, parse_reason

    assert outcome != "merged", f"Unusable comparison must hold as infrastructure, got {outcome!r}"
    assert case.tracker.merged_prs == []
    assert case.tracker.get_issue(1).state == "open"
    assert case.tracker.get_issue(2).state == "open"
    assert "blocked" in case.tracker.get_issue(1).labels
    assert "running" not in case.tracker.get_issue(1).labels
    flag = case.store.load_flag(1)
    assert flag is not None and GROUP.get(flag.reason) == "tech-failure"
    assert flag.reason in [parse_reason(comment.body) for comment in case.tracker.list_comments(1)]
    assert case.store.get_run(1).status == "finished" and case.store.get_run(1).attempts == 1
    assert len(case.implementer.calls) == 1, "Infrastructure must not spend worker repair turns"


def test_checks_config_defaults_local_and_validates_project_choices(tmp_path):
    from foragent.config import ConfigError, load_config

    path, global_path = tmp_path / ".foragent.yml", tmp_path / "missing-global.yml"
    path.write_text("review: fast\n")
    assert getattr(load_config(tmp_path, global_path), "checks", None) == "local"
    for mode in ("local", "ci"):
        path.write_text(yaml.safe_dump({"checks": mode}))
        assert load_config(tmp_path, global_path).checks == mode
    for invalid in ("auto", "", True, None, ["ci"]):
        path.write_text(yaml.safe_dump({"checks": invalid}))
        with pytest.raises(ConfigError):
            load_config(tmp_path, global_path)


def test_checks_fallback_is_explicit_local_opt_in_and_validated(tmp_path):
    from foragent.config import ConfigError, load_config

    path, global_path = tmp_path / ".foragent.yml", tmp_path / "missing-global.yml"
    path.write_text("checks: ci\n")
    assert getattr(load_config(tmp_path, global_path), "checks_fallback", None) is None
    path.write_text("checks: ci\nchecks_fallback: local\n")
    assert load_config(tmp_path, global_path).checks_fallback == "local"
    for invalid in ("ci", "auto", "", True, ["local"]):
        path.write_text(yaml.safe_dump({"checks": "ci", "checks_fallback": invalid}))
        with pytest.raises(ConfigError):
            load_config(tmp_path, global_path)


def init_case(git_history, tmp_path, monkeypatch, *, private, answer):
    from foragent import cli
    from foragent import init as setup

    case = make_case(git_history, tmp_path, monkeypatch, skill_libraries=[])
    monkeypatch.setattr(case.git, "remote_slug", lambda: REPO)
    monkeypatch.setattr(case.git, "commit_paths", lambda *args, **kwargs: None)
    monkeypatch.setattr(setup, "_logged_in", lambda tool: True)
    monkeypatch.setattr(cli, "init_repo", lambda root, **kwargs: setup.init_repo(
        root, git=case.git, tracker=case.tracker, **kwargs))
    case.prompts = []

    def choose(prompt):
        case.prompts.append(prompt)
        return answer

    monkeypatch.setattr("builtins.input", choose)

    def github(args):
        if args == ["auth", "status"]:
            return ""
        if args[:2] == ["repo", "view"]:
            data = {"isPrivate": private, "visibility": "PRIVATE" if private else "PUBLIC",
                    "nameWithOwner": REPO}
            return json.dumps(data)
        if args[:1] == ["api"] and f"repos/{REPO}" in args:
            return json.dumps({"private": private, "full_name": REPO,
                               "visibility": "private" if private else "public"})
        pytest.fail(f"Unrecorded init gh command: {args}")

    case.gh_calls = record_gh(monkeypatch, github)
    case.argv = ["init", "--repo-root", str(case.root), "--no-push", "--no-launchd", "--no-board",
                 "--approver", "human", "--language", "en", "--statusline", "no"]
    case.main = cli.main
    return case


@pytest.mark.parametrize("private,recommended,reason", [(False, "ci", "free"), (True, "local", "minutes")],
                         ids=["public-ci-free", "private-local-paid-minutes"])
def test_init_explains_visibility_recommendation_and_persists_enter_choice(
    git_history, tmp_path, monkeypatch, capsys, private, recommended, reason,
):
    case = init_case(git_history, tmp_path, monkeypatch, private=private, answer="")
    assert case.main(case.argv) == 0
    written = yaml.safe_load((case.root / ".foragent.yml").read_text())
    assert written.get("checks") == recommended, "Enter must accept and persist the visibility-based recommendation"
    output = (capsys.readouterr().out + " ".join(case.prompts)).lower()
    assert ("private" if private else "public") in output and reason in output
    assert "ci" in output and "local" in output and "recommend" in output
    assert case.prompts and case.gh_calls, "Init must discover visibility and offer a human choice"
    assert (case.root / WORKFLOW).exists() is (recommended == "ci")


@pytest.mark.parametrize("private,answer", [(False, "local"), (True, "ci")], ids=["public-override", "private-override"])
def test_init_honors_choice_over_recommendation_and_keeps_it_on_rerun(
    git_history, tmp_path, monkeypatch, private, answer,
):
    case = init_case(git_history, tmp_path, monkeypatch, private=private, answer=answer)
    assert case.main(case.argv) == 0
    path = case.root / ".foragent.yml"
    assert yaml.safe_load(path.read_text()).get("checks") == answer
    assert (case.root / WORKFLOW).exists() is (answer == "ci")
    before = path.read_bytes()
    prompts = len(case.prompts)
    assert case.main(case.argv) == 0
    assert path.read_bytes() == before and len(case.prompts) == prompts, "Init must retain the saved project choice"


def test_init_generates_pr_workflow_with_shards_and_machine_readable_comparison_outputs(
    git_history, tmp_path, monkeypatch,
):
    case = init_case(git_history, tmp_path, monkeypatch, private=False, answer="ci")
    assert case.main(case.argv) == 0
    path = case.root / WORKFLOW
    assert path.is_file(), "Choosing ci must generate the acceptance comparison workflow"
    document = yaml.safe_load(path.read_text())
    events = document.get("on", document.get(True))
    assert "pull_request" in events
    jobs = document["jobs"]
    sharded = [job for job in jobs.values() if "shard" in job.get("strategy", {}).get("matrix", {})]
    assert sharded, "Acceptance must be distributed over a shard matrix"
    for job_config in sharded:
        strategy = job_config["strategy"]
        shards = strategy["matrix"]["shard"]
        assert isinstance(shards, list) and len(shards) > 1 and len(set(shards)) == len(shards)
        assert strategy.get("fail-fast") is False, "One red shard must not cancel evidence from the others"
        output = job_config.get("outputs", {}).get("comparison")
        assert isinstance(output, str) and "steps." in output and ".outputs." in output
        assert job_config.get("steps"), "Each shard must execute and publish its comparison"
    permissions = [document.get("permissions", {}), *(job.get("permissions", {}) for job in jobs.values())]
    assert any(isinstance(value, dict) and value.get("checks") == "write" for value in permissions), (
        "The chosen check-run output transport needs permission to publish shard reports"
    )


@pytest.mark.parametrize("fallback", [None, "local"], ids=["hold-default", "fallback-only-on-demand"])
def test_ci_tick_never_prebuilds_local_acceptance_even_when_fallback_is_enabled(
    git_history, tmp_path, monkeypatch, fallback,
):
    from foragent.scheduler import tick

    case = mode_case(git_history, tmp_path, monkeypatch, fallback=fallback)
    launches = []

    def record_launch(ctx, build, lock):
        launches.append(build)
        lock.close()

    report = tick(tick_context(case, start_runs=False, baseline_launcher=record_launch))
    assert report.skipped is None
    assert launches == [], "checks: ci must bypass the scheduler's eager local baseline builder"
    assert not case.implementer.calls and not case.reviewer.calls


@pytest.mark.parametrize("empty_shard,fallback", [(False, None), (True, None), (False, "local")],
                         ids=["ordinary-failing-base", "complete-empty-shard", "fallback-not-needed"])
def test_complete_ci_comparison_merges_without_local_baseline_and_runs_only_local_only_and_own_tests(
    git_history, tmp_path, monkeypatch, empty_shard, fallback,
):
    case = mode_case(git_history, tmp_path, monkeypatch, fallback=fallback)
    if empty_shard:
        case.ci.damage = "empty-shard"
    assert run(case) == "merged"
    assert_remote_only(case)
    assert case.ci.reads, "A green ordinary CI bucket is not an acceptance comparison"
    assert case.processes.head_ids == {LOCAL, LOCAL_ACCEPTANCE, OWN, OWN_ACCEPTANCE}, (
        "Select machine-only tests across acceptance and units, and the changed module's own tests; "
        f"observed {case.processes.head_ids}"
    )
    assert len(case.implementer.calls) == 1, "Tests already failing on the base must not spend a repair turn"
    assert case.tracker.merged_prs == [2]
    comparison_record(case, "ci")


@pytest.mark.parametrize("regression,fallback", [("nonpassing", None), ("deselected", None), ("nonpassing", "local")],
                         ids=["failed-or-skipped", "deselected", "fallback-cannot-waive-regression"])
def test_ci_lost_passing_id_returns_to_worker_and_requires_fresh_head_comparison(
    git_history, tmp_path, monkeypatch, regression, fallback,
):
    case = mode_case(git_history, tmp_path, monkeypatch, fallback=fallback)
    case.ci.regression = regression

    def repair(kwargs):
        (kwargs["worktree"] / "src/demo.py").write_text("VALUE = 3\n")
        case.ci.repaired = True
        return "Repaired the reported acceptance regression"

    case.implementer.steps.append(repair)
    case.reviewer.steps.append("VERDICT: PASS")
    assert run(case) == "merged"
    assert len(case.implementer.calls) == 2, "CI's base-pass/head-nonpass must receive the same repair as local D53"
    prompt = case.implementer.calls[1]["prompt"]
    assert KEEP in prompt and FUTURE not in prompt and LOCKED not in prompt
    assert len({head for head, _ in case.ci.reads}) >= 2, "Repair requires a report for the newly published head"
    assert_remote_only(case)


@pytest.mark.parametrize("damage", [
    "missing", "partial", "unparseable", "duplicate", "stale-head", "stale-base",
    "foreign-repository", "shard-count", "incomplete-base", "incomplete-head",
    "missing-exclusions", "extra-exclusions", "unknown-schema", "uncollected-pass", "unfinished-check",
])
def test_green_ci_without_usable_all_shard_comparison_holds_unprotected_merge(
    git_history, tmp_path, monkeypatch, damage,
):
    case = mode_case(git_history, tmp_path, monkeypatch)
    case.ci.damage = damage
    outcome = run(case)
    assert_infrastructure_hold(case, outcome)
    assert case.ci.reads, "Look for acceptance evidence even when all ordinary checks are green"
    assert_remote_only(case)


def test_ci_machine_only_failure_still_blocks_merge_locally(git_history, tmp_path, monkeypatch):
    case = mode_case(git_history, tmp_path, monkeypatch)
    implement = case.implement

    def break_machine(kwargs):
        implement(kwargs)
        (kwargs["worktree"] / "broken-machine").write_text("machine check fails\n")

    case.implementer.steps = [break_machine, break_machine]
    case.reviewer.steps = ["VERDICT: PASS", "VERDICT: PASS"]
    assert run(case) != "merged", "Remote comparison cannot waive a local_only failure"
    assert LOCAL in case.processes.head_ids and case.tracker.merged_prs == []
    assert_remote_only(case)


@pytest.mark.parametrize("kind", ["never-started", "billing-stop"])
def test_ci_unavailable_infrastructure_holds_without_local_capture_or_worker_retry(
    git_history, tmp_path, monkeypatch, kind,
):
    case = mode_case(git_history, tmp_path, monkeypatch)
    case.ci.damage = "missing"
    if kind == "never-started":
        case.ci.never_started = True
    else:
        case.ci.legacy.passing = False
        case.ci.legacy.jobs = [job()]
    outcome = run(case)
    assert_infrastructure_hold(case, outcome)
    assert_remote_only(case)
    if kind == "billing-stop":
        assert any(ANNOTATION in comment.body for comment in case.tracker.list_comments(1))


@pytest.mark.parametrize("kind", ["missing", "billing-stop", "never-started"])
def test_opt_in_local_fallback_protects_merge_and_records_actual_location(
    git_history, tmp_path, monkeypatch, kind,
):
    case = mode_case(git_history, tmp_path, monkeypatch, fallback="local")
    case.ci.damage = "missing"
    if kind == "billing-stop":
        case.ci.legacy.passing = False
        case.ci.legacy.jobs = [job()]
    elif kind == "never-started":
        case.ci.never_started = True
    assert run(case) == "merged", "Opt-in fallback may replace unavailable infrastructure with a usable local comparison"
    assert case.processes.captures, "Fallback must actually capture the base, not trust green checks"
    assert all(call.worker_calls >= 1 for call in case.processes.captures), "Fallback must not become an eager baseline build"
    assert {KEEP, OTHER} <= case.processes.head_ids, "Fallback must verify the base's passing IDs on the head"
    comparison_record(case, "local")


def test_opt_in_fallback_with_unusable_local_report_still_holds(git_history, tmp_path, monkeypatch):
    case = mode_case(git_history, tmp_path, monkeypatch, fallback="local")
    case.ci.damage = "partial"
    case.processes.unusable = True
    outcome = run(case)
    assert_infrastructure_hold(case, outcome)
    assert case.ci.reads and case.processes.captures, "Try the configured fallback only after unusable CI evidence"
    assert all(call.worker_calls == 1 for call in case.processes.captures)


def test_ci_missing_report_cannot_silently_trust_an_existing_local_baseline_cache(
    git_history, tmp_path, monkeypatch,
):
    from foragent.run_baseline import _capture_acceptance_baseline

    case = mode_case(git_history, tmp_path, monkeypatch, mode="local")
    baseline = _capture_acceptance_baseline(1, case.ctx, TEST_COMMAND, "origin/main")
    assert KEEP in baseline.node_ids, "Warm a usable cache using the existing local baseline producer"
    before = len(case.processes.captures)
    select_mode(case, "ci", None)
    case.ci.damage = "missing"
    assert_infrastructure_hold(case, run(case))
    assert case.ci.reads and len(case.processes.captures) == before
    assert not ({KEEP, OTHER} & case.processes.head_ids), "A warm local cache cannot substitute for missing CI proof"


def test_opt_in_local_fallback_still_repairs_previously_passing_test_regressions(
    git_history, tmp_path, monkeypatch,
):
    case = mode_case(git_history, tmp_path, monkeypatch, fallback="local")
    case.ci.damage = "missing"
    implement = case.implement

    def break_keep(kwargs):
        implement(kwargs)
        (kwargs["worktree"] / "broken-keep").write_text("regression\n")

    def repair(kwargs):
        (kwargs["worktree"] / "broken-keep").unlink()
        return "Fixed the fallback comparison regression"

    case.implementer.steps = [break_keep, repair]
    case.reviewer.steps.append("VERDICT: PASS")
    assert run(case) == "merged"
    assert len(case.implementer.calls) == 2 and KEEP in case.implementer.calls[1]["prompt"]
    assert case.ci.reads and case.processes.captures
    assert all(call.worker_calls >= 1 for call in case.processes.captures)
    comparison_record(case, "local")


@pytest.mark.parametrize("mode", [None, "local"], ids=["default", "explicit-local"])
def test_local_mode_keeps_capture_cache_regression_repair_and_records_local_comparison(
    git_history, tmp_path, monkeypatch, mode,
):
    case = mode_case(git_history, tmp_path, monkeypatch, mode=mode)
    implement = case.implement

    def break_keep(kwargs):
        implement(kwargs)
        (kwargs["worktree"] / "broken-keep").write_text("regression\n")

    def repair(kwargs):
        (kwargs["worktree"] / "broken-keep").unlink()
        return "Restored the passing acceptance test"

    case.implementer.steps = [break_keep, repair]
    case.reviewer.steps.append("VERDICT: PASS")
    assert run(case) == "merged"
    assert len(case.processes.captures) == 1 and len(case.implementer.calls) == 2
    assert all(call.worker_calls == 0 for call in case.processes.captures)
    assert KEEP in case.implementer.calls[1]["prompt"] and FUTURE not in case.implementer.calls[1]["prompt"]
    assert {KEEP, OTHER} <= case.processes.head_ids
    assert not case.ci.reads, "Default/local mode must keep today's local comparison path"
    assert list((case.store.root.parent / "baselines").glob("*.json")), "Local baseline caching must remain enabled"
    comparison_record(case, "local")


