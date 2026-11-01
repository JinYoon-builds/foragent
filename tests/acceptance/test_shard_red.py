"""Locked shard-red acceptance; do not edit during implementation.

Reject green lost tests, failing measurement before artifact upload, treating an
absent legacy output as loss, hiding red acceptance jobs/logs, accepting damaged
evidence, or bypassing the existing trusted-workflow rule. Real CLI YAML,
ci_shard.main, tiny pytest measurements and GhTracker readers make decisions.
Only Actions uploads, GitHub transport and model turns are offline fakes.
"""

import ast
from copy import deepcopy
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile

import pytest
import yaml

from conftest import git_command
from foragent import ci_mode, ci_shard, cli, runner
from foragent.ci_workflow import EXCLUSIONS_TRAILER, WORKFLOW, measurement_scope
from foragent.config import Config, load_config
from foragent.tracker import GhTracker, TrackerError
from test_ci_base_cache_workflow import EXPRESSION, UNKNOWN, condition, event_context
from test_ci_base_cache import shard_case  # noqa: F401 -- existing real measurement fixture
from test_ci_mode import (
    ComparisonCI, EXCLUDED, KEEP, OTHER, FUTURE, LOCKED, ci_evidence_recording,
    mode_case, no_external_execution,  # noqa: F401
)
from test_ci_not_started import REPO, RUN, RUN_URL
from test_ci_mode_unit import compare, recorded_github, report
from test_local_truth import record_gh
from test_skills_autosync import cli_exit, isolated_home  # noqa: F401
from test_state_split import offline_acceptance  # noqa: F401


ROOT = next(p for p in Path(__file__).resolve().parents
            if (p / ".foragent.yml").is_file() and (p / "src/foragent/ci_shard.py").is_file())
NODE_FILE = "tests/acceptance/test_shard_subject.py"
NODE = NODE_FILE + "::test_subject"


class Outputs(dict):
    """GitHub expressions read an absent step output as an empty string."""
    def get(self, key, default=None):
        return super().get(key, "")


def value(expression, context):
    """Reuse the repo's expression evaluator, extending numeric/JSON expressions.

    This parses workflow syntax in a test; it does not interpret payload meaning
    or substitute an admission/comparison decision.
    """
    result = condition(expression, context)
    if result is not UNKNOWN:
        return result
    source = expression.strip().removeprefix("${{").removesuffix("}}").strip()
    source = re.sub(r"!(?!=)", "not ", source.replace("&&", " and ").replace("||", " or ")).strip()

    def visit(node):
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name):
            return context.get(node.id, {"true": True, "false": False, "null": None}.get(node.id, UNKNOWN))
        if isinstance(node, ast.Attribute):
            parent = visit(node.value)
            return parent.get(node.attr, UNKNOWN) if isinstance(parent, dict) else UNKNOWN
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            functions = {"fromJSON": json.loads, "toJSON": json.dumps,
                         "success": lambda: True, "always": lambda: True,
                         "failure": lambda: False, "cancelled": lambda: False}
            if node.func.id in functions:
                return functions[node.func.id](*(visit(arg) for arg in node.args))
        if isinstance(node, ast.BoolOp):
            if isinstance(node.op, ast.And):
                return all(visit(arg) for arg in node.values)
            return any(visit(arg) for arg in node.values)
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            return not visit(node.operand)
        if isinstance(node, ast.Compare) and len(node.ops) == 1:
            a, b, op = visit(node.left), visit(node.comparators[0]), node.ops[0]
            if isinstance(op, ast.Eq): return a == b
            if isinstance(op, ast.NotEq): return a != b
            a, b = float(a or 0), float(b or 0)
            if isinstance(op, ast.Gt): return a > b
            if isinstance(op, ast.GtE): return a >= b
            if isinstance(op, ast.Lt): return a < b
            if isinstance(op, ast.LtE): return a <= b
        raise AssertionError(f"Unsupported generated expression: {source}")

    return visit(ast.parse(source, mode="eval").body)


def render(text, context):
    def replace(match):
        result = value(match.group(1), context)
        assert result is not UNKNOWN, match.group(0)
        return str(result).lower() if isinstance(result, bool) else str(result)
    return EXPRESSION.sub(replace, str(text))


def generate(root, monkeypatch, settings):
    root.mkdir(exist_ok=True)
    (root / ".foragent.yml").write_text(yaml.safe_dump(settings))
    record_gh(monkeypatch, lambda args: json.dumps({"isPrivate": False}))
    assert cli_exit(cli.main, ["ci", "workflow", "--repo-root", str(root)]) == 0
    text = (root / WORKFLOW).read_text()
    return yaml.safe_load(text), text


def measurement_job(document):
    jobs = [job for job in document["jobs"].values()
            if "shard" in job.get("strategy", {}).get("matrix", {})]
    assert len(jobs) == 1
    job, = jobs
    assert job["strategy"].get("fail-fast") is False
    assert not job.get("continue-on-error", False)
    steps = job["steps"]
    indices = [i for i, step in enumerate(steps) if "foragent.ci_shard" in step.get("run", "")]
    assert len(indices) == 1
    return job, indices[0]


def run_tail(document, outputs, artifact, directory):
    """Execute the generated post-measurement shell; record uploads at their real position."""
    job, index = measurement_job(document)
    steps = job["steps"]
    upload_indices = [i for i, step in enumerate(steps)
                      if step.get("uses", "").startswith("actions/upload-artifact@")]
    assert len(upload_indices) == 1
    upload, = upload_indices
    assert index < upload
    assert any("run" in step for step in steps[upload + 1:]), (
        "The regression verdict must be a final shell step after artifact upload"
    )
    context = event_context("pull_request", "main")
    context["runner"]["temp"] = str(directory)
    context["steps"] = {steps[index]["id"]: {"outputs": Outputs(outputs), "outcome": "success",
                                               "conclusion": "success"}}
    assert "git+https://github.com/JinYoon-builds/foragent" in steps[index]["run"]
    assert "--no-project" in steps[index]["run"], "Use the tool from trusted main, outside the PR project"
    for comparison in ("", "true"):
        for lost in ("", "0", "1", "12"):
            check_context = deepcopy(context)
            check_context["steps"][steps[index]["id"]]["outputs"] = Outputs(comparison=comparison, lost=lost)
            assert bool(value(steps[upload].get("if", True), check_context)) is bool(comparison), (
                "The artifact-upload condition must stay independent of lost", steps[upload],
            )
    events, uploaded, status, failed_log = [], [], 0, ""
    workspace = Path(os.environ.get("FA_CHECKOUT", directory))
    context["github"]["workspace"] = str(workspace)
    # Compare-step env is not inherited by a later Actions step. In particular,
    # FA_REPORT_FILE must be explicitly supplied or resolved from runner.temp.
    host_environment = dict(os.environ)
    for name in steps[index].get("env", {}):
        host_environment.pop(name, None)
    for i, step in enumerate(steps[index + 1:], index + 1):
        environment = {**document.get("env", {}), **job.get("env", {}), **step.get("env", {})}
        environment = {name: render(content, context) for name, content in environment.items()}
        local = {**context, "env": environment}
        enabled = value(step.get("if", True), local)
        assert enabled is not UNKNOWN
        if not enabled:
            continue
        assert not step.get("continue-on-error", False), "A regression verdict must make the job red"
        if i == upload:
            assert artifact.exists() and step["with"].get("if-no-files-found") == "error"
            assert Path(render(step["with"]["path"], local)) == artifact
            assert render(step["with"]["name"], local) == "acceptance-0"
            uploaded.append(artifact.read_bytes())
            events.append("upload")
        elif "run" in step:
            output = directory / f"step-{i}-outputs"
            defaults = {**document.get("defaults", {}).get("run", {}),
                        **job.get("defaults", {}).get("run", {})}
            cwd = Path(render(step.get("working-directory", defaults.get("working-directory", str(workspace))), local))
            if not cwd.is_absolute():
                cwd = workspace / cwd
            result = subprocess.run(["/bin/bash", "--noprofile", "--norc", "-eo", "pipefail", "-c",
                                     render(step["run"], local)], cwd=cwd,
                                    env={**host_environment, **environment, "GITHUB_OUTPUT": str(output),
                                         "GITHUB_WORKSPACE": str(workspace), "RUNNER_TEMP": str(directory)},
                                    capture_output=True, text=True, check=False)
            events.append("verdict")
            status = result.returncode
            if status:
                failed_log = result.stdout + result.stderr
                break
        else:
            pytest.fail(f"Unexpected post-measurement action: {step}")
    return status, uploaded, events, failed_log


def assert_lost_ids_in_failed_step(payload, status, failed_log):
    lost = set(payload["base"]["passing_ids"]) - set(payload["head"]["passing_ids"])
    assert lost and status != 0
    assert all(node in failed_log for node in lost), (
        "The failing step itself must print every lost test ID for pr_failed_logs and worker repair",
        sorted(lost), failed_log,
    )


def real_measurement(git_history, tmp_path, monkeypatch, *, before, after, excluded=False,
                     before_files=None, after_files=None):
    tmp_path.mkdir(parents=True, exist_ok=True)
    settings = {"repo": REPO, "checks": "ci", "ci_shards": 1,
                "test_command": f"{shlex.quote(sys.executable)} -m pytest"}
    document, text = generate(tmp_path / "workflow", monkeypatch, settings)
    git_history.update({".foragent.yml": yaml.safe_dump(settings), WORKFLOW: text,
                        **(before_files or {NODE_FILE: f"def test_subject(): assert {before!r}\n"})})
    base = git_history.tip
    exclusions = [NODE_FILE] if excluded else []
    git_history.update(after_files or {NODE_FILE: f"def test_subject(): assert {after!r}\n"},
                       message=f"Subject\n\n{EXCLUSIONS_TRAILER}: {json.dumps(exclusions)}")
    head = git_history.tip
    git_command(git_history.clone, "fetch", "-q", "origin")
    git_command(git_history.clone, "merge", "-q", "--ff-only", "origin/main")
    artifact, output = tmp_path / "shard-0.json", tmp_path / "measurement-outputs"
    for name, content in {"FA_BASE_SHA": base, "FA_HEAD_SHA": head, "FA_REPOSITORY": REPO,
                          "FA_CHECKOUT": str(git_history.clone), "FA_SHARD": "0", "FA_SHARDS": "1",
                          "FA_REPORT_FILE": str(artifact), "GITHUB_OUTPUT": str(output),
                          "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}.items():
        monkeypatch.setenv(name, content)
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    try:
        result = ci_shard.main()
    except SystemExit as exc:
        result = exc.code
    assert result in (None, 0), "Measurement must succeed so its artifact can be uploaded"
    payload = json.loads(artifact.read_text())
    assert payload["base"]["complete"] and payload["head"]["complete"]
    assert payload["base_sha"] == base and payload["head_sha"] == head
    outputs = dict(line.split("=", 1) for line in output.read_text().splitlines())
    assert outputs.get("comparison") == "true"
    return document, artifact, outputs, payload


@pytest.mark.parametrize("before,after,excluded,lost", [
    pytest.param(False, False, False, 0, id="preexisting-failure-stays-green"),
    pytest.param(False, True, False, 0, id="fixed-test-stays-green"),
    pytest.param(True, False, True, 0, id="excluded-locked-test-stays-green"),
    pytest.param(True, False, False, 1, id="lost-test-red-after-upload"),
])
def test_real_measurement_and_generated_workflow_verdict(
    git_history, tmp_path, monkeypatch, before, after, excluded, lost,
):
    document, artifact, outputs, payload = real_measurement(
        git_history, tmp_path, monkeypatch, before=before, after=after, excluded=excluded,
    )
    actual_lost = set(payload["base"]["passing_ids"]) - set(payload["head"]["passing_ids"])
    assert actual_lost == ({NODE} if lost else set())
    assert outputs.get("lost") == str(lost), "Export the count of lost passing IDs, including explicit zero"
    status, uploaded, events, failed_log = run_tail(document, outputs, artifact, tmp_path)
    assert bool(status) is bool(lost)
    assert uploaded == [artifact.read_bytes()] and events[0] == "upload"
    if lost:
        assert events.index("upload") < events.index("verdict")
        assert_lost_ids_in_failed_step(payload, status, failed_log)


@pytest.mark.parametrize("legacy", ["absent", "empty"])
def test_introducing_pr_bootstraps_when_trusted_main_has_no_lost_output(tmp_path, monkeypatch, legacy):
    document, _ = generate(tmp_path / "workflow", monkeypatch, {"repo": REPO, "checks": "ci"})
    artifact = tmp_path / "shard-0.json"
    artifact.write_text('{"legacy_measurement":true}')
    outputs = {"comparison": "true"}
    if legacy == "empty": outputs["lost"] = ""
    status, uploaded, _, _ = run_tail(document, outputs, artifact, tmp_path)
    assert status == 0 and uploaded == [artifact.read_bytes()]


def test_committed_workflow_matches_public_generation(tmp_path, monkeypatch):
    settings = yaml.safe_load((ROOT / ".foragent.yml").read_text())
    document, generated = generate(tmp_path / "workflow", monkeypatch, settings)
    assert (ROOT / WORKFLOW).read_text() == generated
    measurement_job(document)


def test_measurement_job_has_a_time_limit_well_under_the_six_hour_default(tmp_path, monkeypatch):
    settings = yaml.safe_load((ROOT / ".foragent.yml").read_text())
    document, _ = generate(tmp_path / "workflow", monkeypatch, settings)
    jobs = [job for job in document["jobs"].values()
            if "shard" in job.get("strategy", {}).get("matrix", {})]
    assert len(jobs) == 1
    limit = jobs[0].get("timeout-minutes")
    assert isinstance(limit, int) and 0 < limit <= 30


def test_cache_acceptance_fixture_accepts_extended_real_measurement_outputs(shard_case):
    """Keep output compatibility independent of the unfinished cache producer feature."""
    payload = shard_case.invoke(cache=False)
    outputs = dict(line.split("=", 1) for line in Path(os.environ["GITHUB_OUTPUT"]).read_text().splitlines())
    assert outputs.get("comparison") == "true"
    lost = set(payload["base"]["passing_ids"]) - set(payload["head"]["passing_ids"])
    assert outputs.get("lost") == str(len(lost))


@pytest.mark.parametrize("conclusion", ["success", "failure", "cancelled", "timed_out", "neutral", None])
def test_completed_measurement_artifacts_are_read_regardless_of_conclusion(conclusion):
    values = [report(), report(1)]
    for item in values:
        item["run"]["conclusion"] = conclusion
    values[0]["report"]["head"]["passing_ids"] = []
    github, calls = recorded_github(values)
    reports = GhTracker(REPO, github).pr_acceptance_reports("head")
    assert len(reports) == 2 and any(args[-1].endswith("/zip") for args in calls)
    assert reports[0]["run"]["conclusion"] == conclusion


@pytest.mark.parametrize("conclusion", ["failure", "cancelled", "timed_out", "neutral", None])
def test_comparison_uses_completed_red_measurements_instead_of_rejecting_the_envelope(conclusion):
    values = [report(), report(1)]
    for item in values:
        item["run"]["conclusion"] = conclusion
    values[0]["report"]["head"]["passing_ids"] = []
    assert compare(values)[0] == [values[0]["report"]["base"]["passing_ids"][0]]


@pytest.mark.parametrize("damage", ["foreign-run", "expired", "missing-shard", "incomplete-head", "foreign-repository"])
def test_red_measurement_still_refuses_unusable_artifacts(damage):
    values = [report(), report(1)]
    for item in values: item["run"]["conclusion"] = "failure"
    if damage == "foreign-run": values[1]["artifact"]["workflow_run"]["id"] = 99
    elif damage == "expired": values[1]["artifact"]["expired"] = True
    elif damage == "missing-shard": values.pop()
    elif damage == "incomplete-head": values[1]["report"]["head"]["complete"] = False
    else: values[0]["run"]["head_repository"]["full_name"] = "fork/repo"
    github, _ = recorded_github(values)
    with pytest.raises((TrackerError, ci_mode.UnusableComparison)):
        compare(GhTracker(REPO, github).pr_acceptance_reports("head"))


class RedCI(ComparisonCI):
    """Acceptance jobs fail with logs; unrelated jobs stay green. Actual GhTracker decides status."""
    failure_log = "Acceptance comparison lost passing test: " + KEEP
    lost_ids = frozenset({KEEP})

    def payload(self, index):
        result = super().payload(index)
        if self.regression and not self.repaired:
            result["head"]["passing_ids"] = [node for node in result["head"]["passing_ids"]
                                              if node not in self.lost_ids]
        return result

    def measurement_run(self):
        result = super().measurement_run()
        result["conclusion"] = "failure" if self.regression and not self.repaired else "success"
        return result

    def check_runs(self):
        run = self.measurement_run()
        return [{"id": 9100 + i, "name": f"acceptance ({i})", "head_sha": run["head_sha"],
                 "status": "completed", "conclusion": run["conclusion"],
                 "app": {"slug": "github-actions"}, "check_suite": {"id": run["check_suite_id"]},
                 "details_url": run["html_url"] + f"/job/{9100 + i}"}
                for i in range(self.case.config.ci_shards)]

    def command(self, args, input=None):
        checks = self.check_runs()
        if args[:2] == ["pr", "checks"]:
            result = [{"name": c["name"], "workflow": "foragent-acceptance", "link": c["details_url"],
                       "bucket": "fail" if c["conclusion"] == "failure" else "pass"} for c in checks]
            result.append({"name": "ordinary", "workflow": "ci", "link": RUN_URL, "bucket": "pass"})
            if any(c["bucket"] == "fail" for c in result):
                raise subprocess.CalledProcessError(1, ["gh", *args], output=json.dumps(result), stderr="")
            return json.dumps(result)
        if args[:2] == ["run", "view"] and args[2] == str(self.run_id) and "--log-failed" in args:
            return self.failure_log
        endpoint = next((arg for arg in args if arg.startswith("repos/")), "")
        if endpoint.split("?", 1)[0] == f"repos/{REPO}/actions/runs/{self.run_id}/jobs":
            jobs = [{"id": c["id"], "name": c["name"], "conclusion": c["conclusion"],
                     "html_url": c["details_url"],
                     "check_run_url": f"repos/{REPO}/check-runs/{c['id']}",
                     "steps": [{"name": "Compare", "conclusion": "success"},
                               {"name": "Upload artifact", "conclusion": "success"},
                               {"name": "Regression verdict", "conclusion": c["conclusion"]}]} for c in checks]
            return json.dumps(jobs if "--jq" in args else {"jobs": jobs, "total_count": len(jobs)})
        if endpoint.startswith(f"repos/{REPO}/check-runs/") and endpoint.endswith("/annotations"):
            return "[]"
        return super().command(args, input)


def red_case(git_history, tmp_path, monkeypatch):
    case = mode_case(git_history, tmp_path, monkeypatch)
    case.ci = RedCI(case, monkeypatch)
    case.ci.regression = "nonpassing"
    return case


def ready(case, head):
    path = case.store.root / "merge-queue.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
    return [r for r in rows if r.get("n") == 1 and r.get("event") == "ready" and r.get("head_sha") == head]


def test_red_acceptance_jobs_and_their_logs_are_ordinary_ci_failures(git_history, tmp_path, monkeypatch):
    case = red_case(git_history, tmp_path, monkeypatch)
    assert case.ctx.tracker.pr_checks(2) == "fail", "Do not filter acceptance jobs out of ordinary CI"
    logs = case.ctx.tracker.pr_failed_logs(2)
    assert KEEP in logs and not logs.not_started, "Preserve regression-step logs for existing worker repair"


def test_red_with_artifacts_repairs_through_existing_ci_failure_path_without_queueing(
    git_history, tmp_path, monkeypatch,
):
    before_files = {
        KEEP.split("::", 1)[0]: "def test_keep(): assert True\n",
        OTHER.split("::", 1)[0]: (
            "import pytest\nclass TestOther:\n"
            "    @pytest.mark.parametrize('value', [1], ids=['value'])\n"
            "    def test_other(self, value): assert True\n"
        ),
    }
    after_files = {path: source.replace("assert True", "assert False")
                   for path, source in before_files.items()}
    with monkeypatch.context() as measurement_patch:
        directory = tmp_path / "measurement"
        document, artifact, outputs, payload = real_measurement(
            git_history, directory, measurement_patch, before=True, after=False,
            before_files=before_files, after_files=after_files,
        )
        assert outputs.get("lost") == "2"
        status, uploaded, events, failed_log = run_tail(document, outputs, artifact, directory)
        assert_lost_ids_in_failed_step(payload, status, failed_log)
        assert uploaded == [artifact.read_bytes()] and events[0] == "upload"
        lost_ids = set(payload["base"]["passing_ids"]) - set(payload["head"]["passing_ids"])
        assert lost_ids == {KEEP, OTHER}
    # Restore the runner fixture's two-shard base config after the one-shard
    # real Actions measurement. The recorded red reports use the same lost IDs.
    git_history.update({".foragent.yml": yaml.safe_dump({
        "repo": REPO, "checks": "ci", "ci_shards": 2,
        "test_command": f"{shlex.quote(sys.executable)} -m pytest",
    })})
    case = red_case(git_history, tmp_path / "run", monkeypatch)
    case.ci.failure_log = failed_log  # Actual final-step stdout/stderr, never invented ID text.
    case.ci.lost_ids = frozenset(lost_ids)
    failed_heads = []

    def repair(kwargs):
        failed_heads.append(case.tracker.pr_head_sha(2))
        assert ready(case, failed_heads[-1]) == [], "A red head cannot join before CI repair"
        (kwargs["worktree"] / "src/demo.py").write_text("VALUE = 3\n")
        case.ci.repaired = True
        return "Repaired the CI-reported acceptance loss"

    case.implementer.steps.append(repair)
    case.reviewer.steps.append("VERDICT: PASS")
    assert runner.run_issue(1, case.ctx) == "merged"
    assert len(case.implementer.calls) == 2 and case.store.get_run(1).attempts == 2
    assert failed_heads and not ready(case, failed_heads[0])
    prompt = case.implementer.calls[1]["prompt"]
    assert "CI failed:" in prompt and all(node in prompt for node in lost_ids)
    assert all(node in case.store.log_path(1).read_text() for node in lost_ids)
    assert case.tracker.merged_prs == [2] and case.ci.artifact_reads
    final_head = case.tracker.pr_head_sha(2)
    assert final_head != failed_heads[0] and ready(case, final_head)
