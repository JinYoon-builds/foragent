"""Optional local-only discovery shares the normal check runner and failure boundary."""

from dataclasses import replace
from datetime import UTC, datetime
import shlex
import sys
from types import SimpleNamespace

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
