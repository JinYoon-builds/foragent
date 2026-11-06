"""Optional local-only discovery shares the normal check runner and failure boundary."""

from dataclasses import replace
from contextlib import contextmanager, nullcontext
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import shlex
import sys
from types import SimpleNamespace
import xml.etree.ElementTree as ET

import pytest

from conftest import git_command
from foragent import runner
from foragent.ci_local import verify_local_only_units
from foragent.config import Config
from foragent.git import GitRepo
from foragent.run_checks import CheckResult, _pytest_command
from foragent.state import StateStore
from foragent.tracker import FakeTracker
from runner_support import run_case


def test_validated_tip_acceptance_receipt_does_not_require_a_unit_receipt(git_history):
    from foragent.ci_local import PassSkips

    node = 'tests/acceptance/test_locked.py::test_passed'
    head = git_history.tip
    reports = [{'run': {'id': 42, 'head_sha': head}, 'report': {
        'head_sha': head, 'repository': 'owner/repo',
        'head': {'complete': True, 'collected_ids': [node], 'passing_ids': [node]},
    }}]
    ctx = SimpleNamespace(repo='owner/repo', git=GitRepo(git_history.clone), tracker=SimpleNamespace(
        pr_default_branch=lambda: 'main',
        pr_unit_passed_tests=lambda head: None, pr_acceptance_reports=lambda head: reports))
    assert PassSkips.read(ctx, head).ci is None
    tip = PassSkips.read(ctx, head, trusted_tip=True)
    assert tip.ci == {node} and tip.run_id == 42
    assert PassSkips.read(ctx, 'other-tip', trusted_tip=True).ci is None
    reports[0]['report']['head']['complete'] = False
    assert PassSkips.read(ctx, head, trusted_tip=True).ci is None


@pytest.mark.parametrize('workflow', ['ci.yml', 'deploy.yaml', 'nested/producer.py'])
@pytest.mark.parametrize('change', ['edit', 'add', 'delete'])
def test_trusted_tip_evidence_requires_every_workflow_to_match_default(git_history, workflow, change):
    from foragent.ci_local import PassSkips

    path = f'.github/workflows/{workflow}'
    if change != 'add':
        git_history.update({path: 'original producer\n'})
    base = git_history.tip
    git_history.update({path: None if change == 'delete' else 'changed producer\n'})
    head = git_history.tip
    git_command(git_history.clone, 'fetch', 'origin')
    git_command(git_history.clone, 'update-ref', 'refs/remotes/origin/main', base)
    node = 'tests/acceptance/test_locked.py::test_passed'
    ctx = SimpleNamespace(repo='owner/repo', git=GitRepo(git_history.clone), tracker=SimpleNamespace(
        pr_default_branch=lambda: 'main', pr_unit_passed_tests=lambda head: (42, {node}),
        pr_acceptance_reports=lambda head: []))
    trusted = PassSkips.read(ctx, base, trusted_tip=True)
    assert trusted.trusted_tip and trusted.ci == {node}
    untrusted = PassSkips.read(ctx, head, trusted_tip=True)
    assert not untrusted.trusted_tip and untrusted.ci is None


def test_owner_selection_imports_checked_code_and_preserves_the_approved_command(attempt, monkeypatch):
    from foragent import ci_local
    from foragent.run_checks import CHECK_ENVIRONMENT

    root = attempt.worktree
    (root / 'pytest.ini').write_text('[pytest]\n')
    (root / 'foragent').mkdir()
    (root / 'foragent/__init__.py').write_text('VALUE = "checked worktree"\n')
    (root / 'tests/test_checked.py').write_text(
        'from foragent import VALUE\ndef test_checked(): assert VALUE == "checked worktree"\n')
    monkeypatch.setattr(ci_local, '_test_slot', lambda *args: nullcontext())
    monkeypatch.setattr(ci_local, '_progress', lambda *args: None)
    command = f'{attempt.ctx.config.test_command} -q -p no:xdist tests/test_checked.py'
    skips = ci_local.PassSkips('tip', None)
    with ci_local.skipping(1, attempt.ctx, command, skips, root=root) as selected:
        assert selected == command
        result = runner._check(selected, root, attempt.ctx.store.log_path(1), 30)
    assert result.exit_code == 0, result.output
    assert skips.ran == {'tests/test_checked.py::test_checked'}
    assert CHECK_ENVIRONMENT.get() == {}


@pytest.mark.parametrize('pythonpath', ['.', 'src'])
@pytest.mark.parametrize('owned', [False, True], ids=['done-when', 'normalized'])
def test_owner_selection_survives_command_pythonpath(attempt, monkeypatch, pythonpath, owned):
    from foragent import ci_local
    from foragent.run_checks import CHECK_ENVIRONMENT

    root = attempt.worktree
    (root / 'pytest.ini').write_text('[pytest]\n')
    source = root / pythonpath
    source.mkdir(exist_ok=True)
    (source / 'sample.py').write_text('VALUE = "checked source"\n')
    (root / 'tests/test_passed.py').write_text('def test_passed(): assert False, "already passed"\n')
    (root / 'tests/test_fresh.py').write_text(
        'import os\ndef test_fresh():\n    from sample import VALUE\n'
        '    assert VALUE == "checked source"\n'
        f'    assert os.environ["PYTHONPATH"] == {pythonpath!r}\n')
    monkeypatch.setattr(ci_local, '_test_slot', lambda *args: nullcontext())
    monkeypatch.setattr(ci_local, '_progress', lambda *args: None)
    command = f'PYTHONPATH={pythonpath} {attempt.ctx.config.test_command}'
    if owned:
        command = _pytest_command(command)
    command += ' -q -p no:xdist tests/test_passed.py tests/test_fresh.py'
    skips = ci_local.PassSkips('tip', frozenset({'tests/test_passed.py::test_passed'}))
    for _ in range(2):
        with ci_local.skipping(1, attempt.ctx, command, skips, root=root, owned=owned) as selected:
            if not owned:
                assert selected == command
            result = runner._check(selected, root, attempt.ctx.store.log_path(1), 30)
        assert result.exit_code == 0, result.output
    assert skips.ran == {'tests/test_fresh.py::test_fresh'}
    assert CHECK_ENVIRONMENT.get() == {}


@pytest.mark.parametrize('trusted_tip', [False, True])
@pytest.mark.parametrize('own_config', [False, True], ids=['parent-config', 'project-config'])
def test_owner_ci_skips_locked_tests_only_on_trusted_tip_and_keeps_machine_checks(
        attempt, tmp_path, monkeypatch, trusted_tip, own_config):
    from foragent import ci_local

    root = attempt.worktree
    if own_config:
        (root / 'pytest.ini').write_text('[pytest]\nmarkers = local_only: machine\n')
    (root / 'tests/acceptance').mkdir()
    receipt = tmp_path / 'receipt'
    portable = 'tests/acceptance/test_locked.py::test_portable'
    machine = 'tests/acceptance/test_locked.py::test_machine'
    (root / 'tests/acceptance/test_locked.py').write_text(
        'import pytest\n'
        f'def record(value):\n    with open({str(receipt)!r}, "a") as stream: stream.write(value + "\\n")\n'
        'def test_portable(): record("portable")\n'
        '@pytest.mark.local_only\ndef test_machine(): record("machine")\n')
    monkeypatch.setattr(ci_local, '_test_slot', lambda *args: nullcontext())
    monkeypatch.setattr(ci_local, '_progress', lambda *args: None)
    skips = ci_local.PassSkips('tip', frozenset({portable, machine}), trusted_tip=trusted_tip)
    command = f'{attempt.ctx.config.test_command} -q -p no:xdist tests/acceptance/test_locked.py'
    for _ in range(2):
        with ci_local.skipping(1, attempt.ctx, command, skips, ['tests/acceptance/test_locked.py'],
                               root=root) as selected:
            result = runner._check(selected, root, attempt.ctx.store.log_path(1), 30)
        assert result.exit_code == 0, result.output
    assert receipt.read_text().splitlines() == (['machine'] if trusted_tip else ['portable', 'machine'])


@pytest.mark.parametrize('tail,expected', [
    ('', 0),
    (' && {python} -m pytest -q -p no:xdist tests/test_uncovered.py', 1),
    (' && {python} -m pytest -q -p no:xdist tests/test_passed.py -k missing', 5),
    (' && {python} -m pytest -q -p no:xdist tests/empty', 5),
    (' && exit 5', 5),
    (' && {python} -m pytest -q -p no:xdist tests/missing.py', 4),
])
def test_owner_only_credits_exit_five_from_its_own_complete_deselection(
        attempt, tmp_path, monkeypatch, tail, expected):
    from foragent import ci_local

    root = attempt.worktree
    (root / 'pytest.ini').write_text('[pytest]\n')
    (root / 'tests/empty').mkdir()
    receipt = tmp_path / 'receipt'
    (root / 'tests/test_passed.py').write_text(
        f'def test_passed():\n    open({str(receipt)!r}, "w").write("rerun")\n')
    (root / 'tests/test_uncovered.py').write_text('def test_uncovered(): assert False, "uncovered failure"\n')
    monkeypatch.setattr(ci_local, '_test_slot', lambda *args: nullcontext())
    monkeypatch.setattr(ci_local, '_progress', lambda *args: None)
    skips = ci_local.PassSkips('tip', frozenset({'tests/test_passed.py::test_passed'}), trusted_tip=True)
    command = f'{attempt.ctx.config.test_command} -q -p no:xdist tests/test_passed.py'
    command += tail.format(python=shlex.quote(sys.executable))
    with ci_local.skipping(1, attempt.ctx, command, skips, root=root) as selected:
        result = runner._check(selected, root, attempt.ctx.store.log_path(1), 30)
    assert result.exit_code == expected, result.output
    assert not receipt.exists()


def test_ci_local_capture_excludes_slot_wait_and_protected_check_runs_once(git_history, tmp_path, monkeypatch):
    from foragent import capacity, run_checks
    from foragent.ci_local import capture_local_only

    now = [datetime.now(UTC)]
    store = StateStore(tmp_path / 'state', now=lambda: now[0])
    root = git_history.clone
    store.start_run(1, branch='test', worktree=root)
    ctx = runner.RunContext('owner/repo', root, FakeTracker(), store, Config(checks='ci'), GitRepo(root),
                            lambda: now[0], monotonic=lambda: now[0].timestamp())
    node = 'tests/acceptance/test_machine.py::test_machine'
    calls = []

    @contextmanager
    def slot(*args, **kwargs):
        now[0] += timedelta(seconds=500)
        yield

    def process(argv, **kwargs):
        calls.append((argv, kwargs['timeout_s']))
        words = shlex.split(argv[-1])
        if node in words:
            limit = kwargs['timeout_s']
            now[0] += timedelta(seconds=min(1000, limit))
            return (143, True, '') if limit < 1000 else (0, False, '')
        assert words[words.index('-m') + 1] == 'local_only'
        report = next(word.split('=', 1)[1] for word in words if word.startswith('--junitxml='))
        evidence = next(word.split('=', 1)[1] for word in words if word.startswith('FA_REPORT='))
        suite = ET.Element('testsuite')
        ET.SubElement(suite, 'testcase', file=node.split('::')[0], name='test_machine')
        ET.ElementTree(suite).write(report, encoding='unicode')
        Path(evidence).write_text(json.dumps({'complete': True, 'exit_code': 0,
                                             'collected_ids': [node], 'passing_ids': [node]}))
        now[0] += timedelta(seconds=736)
        return 0, False, ''

    monkeypatch.setattr(capacity, 'hold_test_slot', slot)
    monkeypatch.setattr(runner, 'run_logged', process)
    baseline = capture_local_only(1, ctx, ctx.git, 'pytest -q', ctx.git.head_sha(), [])

    assert baseline.node_ids == baseline.collected_ids == (node,)
    assert baseline.duration_seconds == 736
    assert len(calls) == 1
    attempt = SimpleNamespace(n=1, ctx=ctx, baseline=baseline, worktree=root,
                              log=store.log_path(1), checks=[])
    assert run_checks._check_protected(attempt).exit_code == 0
    assert len(calls) == 2
    assert calls[-1][1] == 1472


@pytest.fixture
def attempt(tmp_path):
    root = tmp_path / 'repo with spaces'
    (root / 'tests').mkdir(parents=True)
    store = StateStore(tmp_path / 'state')
    store.start_run(1, branch='test', worktree=root)
    config = Config(test_command=f'{shlex.quote(sys.executable)} -m pytest')
    ctx = runner.RunContext('owner/repo', root, FakeTracker(), store, config, GitRepo(root),
                            lambda: datetime.now(UTC))
    return SimpleNamespace(n=1, ctx=ctx, worktree=root)


def test_local_only_discovery_skips_projects_without_marker_without_subprocess(attempt, monkeypatch):
    (attempt.worktree / 'tests/test_sample.py').write_text('from sample import VALUE\n')
    (attempt.worktree / 'pytest.ini').write_text('[pytest]\nmarkers = portable: works anywhere\n')

    def unexpected_process(*args, **kwargs):
        pytest.fail('Marker-free projects must not start discovery subprocesses')

    monkeypatch.setattr('subprocess.Popen', unexpected_process)
    assert verify_local_only_units(attempt, _pytest_command(attempt.ctx.config.test_command)) is None


@pytest.mark.parametrize('path,content', [
    ('tests/test_sample.py', 'pytestmark = pytest.mark.local_only\n'),
    ('tests/conftest.py', '# local_only hook\n'),
    ('conftest.py', '# local_only hook\n'),
    ('pytest.ini', '[pytest]\nmarkers = local_only: machine tools\n'),
    ('.pytest.ini', '[pytest]\nmarkers = local_only: machine tools\n'),
    ('pyproject.toml', '[tool.pytest.ini_options]\nmarkers = ["local_only: machine tools"]\n'),
    ('setup.cfg', '[tool:pytest]\nmarkers = local_only: machine tools\n'),
    ('tox.ini', '[pytest]\nmarkers = local_only: machine tools\n'),
    ('pytest.toml', '[pytest]\nmarkers = ["local_only: machine tools"]\n'),
    ('.pytest.toml', '[pytest]\nmarkers = ["local_only: machine tools"]\n'),
])
def test_local_only_discovery_detects_test_files_and_marker_config(attempt, monkeypatch, path, content):
    (attempt.worktree / path).write_text(content)
    calls = []

    def collect(command, worktree, log, timeout_s, **kwargs):
        calls.append(command)
        return CheckResult(command, 5, False, 'no tests collected')

    monkeypatch.setattr(runner, '_check', collect)
    assert verify_local_only_units(attempt, _pytest_command(attempt.ctx.config.test_command)) is None
    assert len(calls) == 1
    assert '--collect-only' in shlex.split(calls[0])


def test_local_only_collection_uses_related_check_command_directory_and_environment(attempt, monkeypatch):
    source = attempt.worktree / 'source tree'
    source.mkdir()
    (source / 'sample.py').write_text('VALUE = 42\n')
    (attempt.worktree / 'tests/test_sample.py').write_text(
        'import os\nimport pytest\nfrom sample import VALUE\n'
        'pytestmark = pytest.mark.local_only\n'
        'assert "VIRTUAL_ENV" not in os.environ\n'
        'def test_value(): assert str(VALUE) == os.environ["EXPECTED_VALUE"]\n')
    command = _pytest_command(f"PYTHONPATH='source tree' EXPECTED_VALUE=42 "
                              f"{attempt.ctx.config.test_command} --import-mode=importlib")
    monkeypatch.setenv('VIRTUAL_ENV', str(attempt.worktree / 'unrelated-venv'))
    calls = []
    run_logged = runner.run_logged

    def process(argv, **kwargs):
        calls.append((argv, kwargs['cwd'], kwargs['env']))
        return run_logged(argv, **kwargs)

    monkeypatch.setattr(runner, 'run_logged', process)
    local = verify_local_only_units(attempt, command)
    related = runner._check(f'{command} tests/test_sample.py', attempt.worktree,
                            attempt.ctx.store.log_path(1), 900, slot=runner._test_slot(1, attempt.ctx))
    assert local.exit_code == related.exit_code == 0
    assert len(calls) == 3  # discovery, selected local tests, related tests
    for argv, cwd, env in calls:
        assert argv[:2] == ['/bin/sh', '-c']
        assert argv[2].startswith(command + ' ')
        assert cwd == attempt.worktree
        assert env == calls[-1][2]
    assert '--collect-only' in shlex.split(calls[0][0][2])


@pytest.mark.parametrize('failure', [
    CheckResult('collection', 2, False, "ModuleNotFoundError: No module named 'sample'"),
    CheckResult('collection', None, True, 'collection timed out'),
    OSError('collection could not start'),
], ids=['collection-error', 'timeout', 'launch-error'])
def test_local_only_discovery_errors_are_logged_without_failing(attempt, monkeypatch, failure):
    (attempt.worktree / 'pytest.ini').write_text('[pytest]\nmarkers = local_only: machine tools\n')

    def collect(*args, **kwargs):
        if isinstance(failure, OSError):
            raise failure
        return failure

    monkeypatch.setattr(runner, '_check', collect)
    assert verify_local_only_units(attempt, _pytest_command(attempt.ctx.config.test_command)) is None
    log = attempt.ctx.store.log_path(1).read_text()
    assert (str(failure) if isinstance(failure, OSError) else failure.output) in log
    assert 'continuing with related tests' in log


def test_local_only_collection_error_falls_back_to_related_check(run_case):
    case = run_case
    case.history.update({'tests/test_unrelated.py': 'import missing_optional_dependency\n'})
    git_command(case.history.clone, 'fetch', '-q', 'origin')
    git_command(case.history.clone, 'merge', '-q', '--ff-only', 'origin/main')
    case.ctx.config = replace(case.ctx.config, test_command=f'{shlex.quote(sys.executable)} -m pytest')
    case.executor.steps = [{'feature.txt': 'done', 'tests/test_new.py': (
        'import pytest\n@pytest.mark.local_only\ndef test_new(): assert True\n'
    )}]

    assert runner.run_issue(1, case.ctx) == 'merged'
    log = case.store.log_path(1).read_text()
    assert 'Local-only unit collection exit_code=2' in log
    assert 'missing_optional_dependency' in log
    assert 'continuing with related tests' in log
    assert ' tests/test_new.py\n' in log
    assert len(case.executor.calls) == 1
