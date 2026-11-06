"""Unit CI evidence comes only from complete, exact-head JUnit artifacts."""

from copy import deepcopy
from io import BytesIO
import json
import subprocess
import xml.etree.ElementTree as ET
from zipfile import ZipFile

import pytest

from foragent.tracker import FakeTracker, GhTracker


HEAD = "a" * 40
REPO = "owner/repo"
WORKFLOW = "jobs:\n  test:\n    strategy:\n      matrix:\n        shard: [0, 1]\n"


def archive(index, xml, *, tested_commit=HEAD):
    if tested_commit is not None:
        # Add provenance to otherwise valid reports; malformed inputs remain malformed.
        try:
            report = ET.fromstring(xml.encode() if isinstance(xml, str) else xml)
        except (ET.ParseError, LookupError):
            pass
        else:
            for suite in report.iter("testsuite"):
                properties = ET.SubElement(suite, "properties")
                ET.SubElement(properties, "property", name="foragent_tested_commit", value=tested_commit)
            xml = ET.tostring(report)
    stream = BytesIO()
    with ZipFile(stream, "w") as zipped:
        zipped.writestr(f"unit-{index}.xml", xml)
    return stream.getvalue()


@pytest.fixture
def evidence():
    run = dict(id=42, head_sha=HEAD, path=".github/workflows/ci.yml", status="completed",
               conclusion="success", repository=dict(full_name=REPO),
               head_repository=dict(full_name=REPO))
    artifacts = [dict(id=100 + index, name=f"unit-{index}", expired=False,
                      workflow_run=dict(id=42, head_sha=HEAD)) for index in range(2)]
    return dict(runs=[run], workflow=WORKFLOW, artifacts=artifacts, archives=[
        archive(0, '<testsuites><testsuite>'
                '<testcase file="tests/test_sample.py" classname="tests.test_sample" name="test_passed"/>'
                '<testcase file="tests/test_sample.py" classname="tests.test_sample" name="test_skipped">'
                '<skipped/></testcase>'
                '<testcase file="tests/test_sample.py" classname="tests.test_sample" name="test_failed">'
                '<failure/></testcase>'
                '</testsuite></testsuites>'),
        archive(1, '<testsuites><testsuite>'
                '<testcase file="tests/test_other.py" classname="tests.test_other.TestNested.Inner" '
                'name="test_param[an.id]"/>'
                '<testcase file="tests/test_other.py" classname="tests.test_other" name="test_error">'
                '<error/></testcase>'
                '</testsuite></testsuites>'),
    ])


def reader(evidence, *, fail_at=None):
    calls = []

    def run(args, input=None):
        calls.append(args)
        if fail_at is not None and len(calls) == fail_at:
            raise subprocess.CalledProcessError(1, args, stderr="HTTP 403 Forbidden")
        endpoint = args[-1]
        if "/actions/runs?" in endpoint:
            assert f"head_sha={HEAD}" in endpoint
            return json.dumps(evidence["runs"])
        if "/contents/" in endpoint:
            assert endpoint.endswith(f"ci.yml?ref={HEAD}")
            assert "Accept: application/vnd.github.raw+json" in args
            return evidence["workflow"]
        if "/artifacts?" in endpoint:
            assert endpoint.endswith("/runs/42/artifacts?per_page=100")
            return json.dumps(evidence["artifacts"])
        if endpoint.endswith("/zip"):
            return evidence["archives"][int(endpoint.split("/")[-2]) - 100]
        raise AssertionError(args)

    return GhTracker(REPO, run), calls


def test_reads_only_passed_node_ids_with_exact_file_classes_and_parameters(evidence):
    tracker, calls = reader(evidence)
    assert tracker.pr_unit_passed_tests(HEAD) == (42, {
        "tests/test_sample.py::test_passed",
        "tests/test_other.py::TestNested::Inner::test_param[an.id]",
    })
    assert len(calls) == 5


@pytest.mark.parametrize("damage", [
    "no-run", "wrong-sha", "wrong-workflow", "unfinished", "failed", "foreign-repo", "foreign-head",
    "newer-unfinished", "missing-artifact", "missing-shard", "extra-shard", "duplicate-shard",
    "expired", "artifact-sha", "artifact-run", "artifact-id", "malformed-xml", "old-xunit2",
    "bad-zip", "wrong-xml-name", "unsupported-matrix", "bad-workflow", "unknown-xml-encoding",
])
def test_unusable_evidence_falls_back_to_local(evidence, damage):
    run = evidence["runs"][0]
    artifact = evidence["artifacts"][0]
    if damage == "no-run":
        evidence["runs"] = []
    elif damage == "wrong-sha":
        run["head_sha"] = "b" * 40
    elif damage == "wrong-workflow":
        run["path"] = ".github/workflows/other.yml"
    elif damage == "unfinished":
        run["status"] = "in_progress"
    elif damage == "failed":
        run["conclusion"] = "failure"
    elif damage == "foreign-repo":
        run["repository"]["full_name"] = "other/repo"
    elif damage == "foreign-head":
        run["head_repository"]["full_name"] = "other/repo"
    elif damage == "newer-unfinished":
        evidence["runs"].append({**deepcopy(run), "id": 43, "status": "queued"})
    elif damage == "missing-artifact":
        evidence["artifacts"] = []
    elif damage == "missing-shard":
        evidence["artifacts"].pop()
    elif damage == "extra-shard":
        evidence["artifacts"].append({**deepcopy(artifact), "id": 102, "name": "unit-2"})
    elif damage == "duplicate-shard":
        evidence["artifacts"][1]["name"] = "unit-0"
    elif damage == "expired":
        artifact["expired"] = True
    elif damage == "artifact-sha":
        artifact["workflow_run"]["head_sha"] = "b" * 40
    elif damage == "artifact-run":
        artifact["workflow_run"]["id"] = 43
    elif damage == "artifact-id":
        evidence["artifacts"][1]["id"] = artifact["id"]
    elif damage == "malformed-xml":
        evidence["archives"][0] = archive(0, "<testsuites>")
    elif damage == "old-xunit2":
        evidence["archives"][0] = archive(0, '<testsuite><testcase classname="tests.test_sample" '
                                           'name="test_passed"/></testsuite>')
    elif damage == "bad-zip":
        evidence["archives"][0] = b"not a zip"
    elif damage == "wrong-xml-name":
        evidence["archives"][0] = archive(99, "<testsuite/>")
    elif damage == "unsupported-matrix":
        evidence["workflow"] = "jobs: {test: {strategy: {matrix: '${{ fromJSON(inputs.shards) }}'}}}"
    elif damage == "bad-workflow":
        evidence["workflow"] = "[broken yaml"
    elif damage == "unknown-xml-encoding":
        evidence["archives"][0] = archive(0, '<?xml version="1.0" encoding="unknown-charset"?><testsuite/>')
    tracker, _ = reader(evidence)
    assert tracker.pr_unit_passed_tests(HEAD) is None


@pytest.mark.parametrize("fail_at", range(1, 6))
def test_any_artifact_transport_failure_falls_back(evidence, fail_at):
    tracker, _ = reader(evidence, fail_at=fail_at)
    assert tracker.pr_unit_passed_tests(HEAD) is None


def test_conflicting_testcase_cannot_supply_a_pass(evidence):
    evidence["archives"][1] = archive(1, '<testsuite>'
        '<testcase file="tests/test_sample.py" classname="tests.test_sample" name="test_passed">'
        '<skipped/></testcase></testsuite>')
    tracker, _ = reader(evidence)
    assert tracker.pr_unit_passed_tests(HEAD) == (42, set())


@pytest.mark.parametrize("shard", [0, 1])
@pytest.mark.parametrize("tested_commit", [None, "", "b" * 40], ids=["absent", "empty", "merge-commit"])
def test_every_shard_must_record_the_commit_actually_tested(evidence, shard, tested_commit):
    evidence["runs"][0]["event"] = "pull_request"
    # API metadata still points to HEAD even when checkout tested its merge commit.
    evidence["archives"][shard] = archive(shard, '<testsuite>'
        '<testcase file="tests/test_sample.py" classname="tests.test_sample" name="test_passed"/>'
        '</testsuite>', tested_commit=tested_commit)
    tracker, _ = reader(evidence)
    assert tracker.pr_unit_passed_tests(HEAD) is None


@pytest.mark.parametrize("properties", [
    '',
    f'<properties><property name="foragent_tested_commit" value="{"b" * 40}"/></properties>',
    f'<properties><property name="foragent_tested_commit" value="{HEAD}"/>'
    f'<property name="foragent_tested_commit" value="{HEAD}"/></properties>',
], ids=["missing-suite-provenance", "different-suite-commit", "duplicate-property"])
def test_all_suites_need_unambiguous_matching_provenance(evidence, properties):
    matching = f'<properties><property name="foragent_tested_commit" value="{HEAD}"/></properties>'
    evidence["archives"][0] = archive(0,
        f'<testsuites><testsuite>{matching}</testsuite><testsuite>{properties}'
        '<testcase file="tests/test_sample.py" classname="tests.test_sample" name="test_passed"/>'
        '</testsuite></testsuites>', tested_commit=None)
    tracker, _ = reader(evidence)
    assert tracker.pr_unit_passed_tests(HEAD) is None


@pytest.mark.parametrize("tested_commit", [None, "b" * 40], ids=["missing", "merge-commit"])
def test_unverified_tested_commit_runs_every_related_test(evidence, tested_commit, tmp_path, monkeypatch):
    import sys

    from conftest import BASE_TIME
    from foragent.config import Config
    from foragent.git import GitRepo
    from foragent.run_checks import _pytest_command, run_related_check
    from foragent.runner import RunContext
    from foragent.state import StateStore

    file = "tests/test_sample.py"
    root = tmp_path / "tree"
    (root / "tests").mkdir(parents=True)
    (root / file).write_text("def test_passed():\n    assert False, 'fails at the requested head'\n")
    evidence["archives"][0] = archive(0,
        '<testsuite><testcase file="tests/test_sample.py" classname="tests.test_sample" '
        'name="test_passed"/></testsuite>', tested_commit=tested_commit)
    tracker, _ = reader(evidence)
    store = StateStore(tmp_path / "state", now=lambda: BASE_TIME)
    store.start_run(1, branch="test", worktree=root)
    ctx = RunContext(REPO, root, tracker, store, Config(checks="ci", merge_owner=True),
                     GitRepo(root), lambda: BASE_TIME)
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    monkeypatch.delenv("PYTEST_PLUGINS", raising=False)

    result = run_related_check(1, ctx, root, _pytest_command(f"{sys.executable} -m pytest"),
                               [file], 1, 60, ci_passed=tracker.pr_unit_passed_tests(HEAD), head_sha=HEAD)

    assert result.exit_code == 1
    assert "fails at the requested head" in result.output
    assert "--collect-only" not in store.log_path(1).read_text()


def test_ignores_unrelated_runs_and_artifacts(evidence):
    evidence["runs"].extend([
        {**deepcopy(evidence["runs"][0]), "id": 43, "head_sha": "b" * 40},
        {**deepcopy(evidence["runs"][0]), "id": 44, "path": ".github/workflows/acceptance.yml"},
    ])
    evidence["artifacts"].append(dict(id=900, name="acceptance-0"))
    tracker, _ = reader(evidence)
    assert tracker.pr_unit_passed_tests(HEAD)[0] == 42


def test_fake_has_no_remote_unit_evidence():
    assert FakeTracker().pr_unit_passed_tests(HEAD) is None
