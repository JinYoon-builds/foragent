"""CI evidence validation, workflow generation and portable shard measurement."""

from copy import deepcopy
from contextlib import contextmanager
from dataclasses import replace
from datetime import timedelta
from io import BytesIO
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from zipfile import ZipFile

import pytest
import yaml

from foragent.ci_mode import UnusableComparison, comparison
from foragent.ci_workflow import WORKFLOW, measurement_scope, workflow_text, write_workflow
from foragent.config import Config, ConfigError, load_config
from runner_support import run_case


def report(index=0, count=2):
    node = f"tests/acceptance/test_keep_{index}.py::test_keep"
    payload = {"schema_version": 1, "repository": "owner/repo", "base_sha": "base", "head_sha": "head",
               "shard_index": index, "shard_count": count, "excluded": [], "duration_seconds": index + 2,
               "scope": measurement_scope(Config(ci_shards=count), Path('.'), []),
               "base": {"complete": True, "collected_ids": [node], "passing_ids": [node]},
               "head": {"complete": True, "collected_ids": [node], "passing_ids": [node]}}
    run = {"id": 42, "head_sha": "head", "status": "completed", "conclusion": "success",
           "repository": {"full_name": "owner/repo"}, "head_repository": {"full_name": "owner/repo"},
           "path": WORKFLOW, "event": "pull_request"}
    artifact = {"id": 100 + index, "name": f"acceptance-{index}", "expired": False,
                "workflow_run": {"id": 42, "head_sha": "head"}}
    return {"run": run, "artifact": artifact, "report": payload}


def recorded_github(values, *, runs=None):
    """Serve the REST run/artifact shapes and actual binary ZIP responses."""
    calls = []

    def github(args, input=None):
        calls.append(args)
        endpoint = args[-1]
        if endpoint == 'repos/owner/repo':
            return json.dumps({'full_name': 'owner/repo', 'default_branch': 'trunk'})
        if '/actions/runs?head_sha=head&' in endpoint:
            return json.dumps(runs if runs is not None else [values[0]['run'] if values else report()['run']])
        if '/actions/runs/42/artifacts?' in endpoint:
            artifacts = [value['artifact'] for value in values]
            return '\n'.join(json.dumps([artifact]) for artifact in artifacts) or '[]'
        for value in values:
            if endpoint.endswith(f"/actions/artifacts/{value['artifact']['id']}/zip"):
                output = BytesIO()
                with ZipFile(output, 'w') as archive:
                    archive.writestr(f"shard-{value['report']['shard_index']}.json", json.dumps(value['report']))
                return output.getvalue()
        raise AssertionError(f'Unexpected GitHub command: {args}')

    return github, calls


@pytest.mark.parametrize('status', ['queued', 'in_progress'])
@pytest.mark.parametrize('finishes', [True, False])
def test_unfinished_measurement_polls_within_existing_ci_budget(tmp_path, monkeypatch, status, finishes):
    from conftest import BASE_TIME
    from foragent import ci_mode
    from foragent.run_attempt import ExecutorStopped
    from foragent.tracker import GhTracker

    fixtures = Path(__file__).parent / 'fixtures/github_ci'
    run = json.loads((fixtures / 'measurement_run.json').read_text())
    repo, head = run['repository']['full_name'], run['head_sha']
    listing = json.loads((fixtures / 'runs_by_head.json').read_text())
    listing['workflow_runs'] = [run]
    artifacts = json.loads((fixtures / 'artifacts.json').read_text())
    artifacts['artifacts'] = artifacts['artifacts'][:1]
    payload = json.loads((fixtures / 'shard-0.json').read_text())
    config = Config(checks='ci', test_command='pytest', ci_shards=1)
    payload.update(base_sha='base', head_sha=head, excluded=[], shard_count=1,
                   scope=measurement_scope(config, tmp_path, []))
    archive = BytesIO()
    with ZipFile(archive, 'w') as output:
        output.writestr('shard-0.json', json.dumps(payload))
    reads, sleeps, updates = [], [], []
    clock = [BASE_TIME + timedelta(seconds=480)]

    def sleep(seconds):
        sleeps.append(seconds)
        clock[0] += timedelta(seconds=seconds)

    def github(args, input=None):
        endpoint = args[-1]
        if endpoint == f'repos/{repo}':
            return json.dumps({'full_name': repo, 'default_branch': 'main'})
        if '/actions/runs?' in endpoint:
            reads.append(endpoint)
            pending = not finishes or len(reads) <= 8
            run.update(status=status if pending else 'completed', conclusion=None if pending else 'success')
            return json.dumps(listing['workflow_runs'])
        assert finishes and len(reads) == 9, 'Pending runs must not supply artifacts'
        if '/artifacts?' in endpoint:
            return json.dumps(artifacts['artifacts'])
        if endpoint.endswith('/zip'):
            return archive.getvalue()
        raise AssertionError(args)

    ctx = SimpleNamespace(
        repo=repo, repo_root=tmp_path, config=config, now=lambda: clock[0], sleep=sleep,
        tracker=GhTracker(repo, github),
        store=SimpleNamespace(update_run=lambda n, **kwargs: updates.append(kwargs)),
    )
    attempt = SimpleNamespace(
        n=1, ctx=ctx, target_branch='main', log=tmp_path / 'log',
        git=SimpleNamespace(ref_sha=lambda ref: 'trusted', _call=lambda args: ''),
    )
    monkeypatch.setattr(ci_mode, '_baseline_exclusions', lambda *args: [])
    monkeypatch.setattr(ci_mode, 'flag', lambda *args: None)
    monkeypatch.setattr('foragent.run_waits._progress', lambda *args: None)
    monkeypatch.setattr('foragent.runner._remaining', lambda *args: 3600)
    from foragent.run_publish import _CIWait

    deadline = _CIWait(BASE_TIME.timestamp()).deadline
    if finishes:
        evidence = ci_mode.require_comparison(attempt, 2, head, base_sha='base', deadline=deadline)
        assert evidence.reports is not None
        assert any(update.get('comparison', {}).get('source') == 'ci' for update in updates)
        assert len(reads) == 9 and sleeps == [30] * 8
    else:
        with pytest.raises(ExecutorStopped, match='missing acceptance shards'):
            ci_mode.require_comparison(attempt, 2, head, base_sha='base', deadline=deadline)
        assert len(reads) == 25 and sleeps == [30] * 24
        assert clock[0].timestamp() == deadline
        assert not updates


def compare(checks, count=2):
    return comparison(checks, "owner/repo", "base", "head", [],
                      expected_scope=measurement_scope(Config(ci_shards=count), Path('.'), []),
                      expected_shards=range(count))


def test_compare_records_parallel_wall_duration_and_detects_deselection():
    checks = [report(), report(1)]
    payload = checks[1]['report']
    payload["head"] = {"complete": True, "collected_ids": [], "passing_ids": []}
    checks[1]['report'] = payload
    lost, duration = compare(checks)
    assert lost == ["tests/acceptance/test_keep_1.py::test_keep"]
    assert duration == 3


@pytest.mark.parametrize("damage", ["check-sha", "nan", "overlap", "duplicate", "malformed", "cancelled"])
def test_rejects_unusable_shards(damage):
    checks = [report(), report(1)]
    payload = checks[1]['report']
    if damage == "check-sha":
        checks[1]['run']["head_sha"] = "old"
    elif damage == "nan":
        payload["duration_seconds"] = float('nan')
    elif damage == "overlap":
        payload["base"] = checks[0]['report']["base"]
    elif damage == "duplicate":
        checks[1] = deepcopy(checks[0])
        payload = checks[1]['report']
    elif damage == "cancelled":
        checks[1]['run']["conclusion"] = "cancelled"
    checks[1]['report'] = '{' if damage == 'malformed' else payload
    with pytest.raises(UnusableComparison):
        compare(checks)


@pytest.mark.parametrize("key,value", [
    ("ci_shards", 0), ("ci_shards", True), ("ci_shards", 257),
    ("ci_runs_on", []), ("ci_runs_on", ["linux", 2]), ("ci_runs_on", None),
    ("ci_runs_on", ""), ("ci_runs_on", True), ("checks_fallback", []),
])
def test_config_rejects_invalid_ci_settings(tmp_path, key, value):
    (tmp_path / '.foragent.yml').write_text(yaml.safe_dump({key: value}))
    with pytest.raises(ConfigError):
        load_config(tmp_path, tmp_path / 'no-global')


@pytest.mark.parametrize("private,warning", [(False, "Warning:"), (True, ""), (None, "Note:")])
def test_workflow_runner_visibility_warning_and_rewrite(tmp_path, capsys, private, warning):
    calls = []

    def github(args, input=None):
        calls.append(args)
        return json.dumps({"isPrivate": private})

    config = Config(ci_shards=3, ci_runs_on=["self-hosted", "linux"])
    path = write_workflow(tmp_path, config, repo="owner/repo", run=github)
    assert warning in capsys.readouterr().err
    document = yaml.safe_load(path.read_text())
    job = document['jobs']['acceptance']
    assert job['runs-on'] == ['self-hosted', 'linux']
    assert job['strategy'] == {'fail-fast': False, 'matrix': {'shard': [0, 1, 2]}}
    assert [step.get('uses') for step in job['steps'][:2]] == ['actions/checkout@v4', 'astral-sh/setup-uv@v6']
    assert document['permissions'] == {'contents': 'read'}
    assert not (tmp_path / '.github/workflows/foragent-acceptance-publish.yml').exists()
    path.write_text('obsolete workflow')
    write_workflow(tmp_path, config, warn=False)
    assert path.read_text() == workflow_text(config)
    assert len(calls) == 1


@pytest.mark.parametrize('generated', [True, False])
def test_workflow_removes_only_generated_legacy_publisher(tmp_path, capsys, generated):
    legacy = tmp_path / '.github/workflows/foragent-acceptance-publish.yml'
    legacy.parent.mkdir(parents=True)
    original = ('# Generated by fa ci workflow.\n' if generated else '# Maintained by the team\n') + (
        'name: foragent-acceptance-publish\njobs:\n  publish:\n'
        '    steps:\n      - run: python -m foragent.ci_publish\n')
    legacy.write_text(original)
    write_workflow(tmp_path, Config(), warn=False)
    assert legacy.exists() is not generated
    if generated:
        assert f'Removed: {legacy.relative_to(tmp_path)}' in capsys.readouterr().out
    else:
        assert legacy.read_text() == original
        assert 'Removed:' not in capsys.readouterr().out


def test_measurement_partitions_all_portable_nodes_and_excludes_marked_and_locked_tests(tmp_path, monkeypatch):
    from foragent.ci_shard import measure

    root = tmp_path / 'repo'
    root.mkdir()
    tests = root / 'tests/acceptance'
    tests.mkdir(parents=True)
    (tests / 'test_portable.py').write_text('import pytest\n'
        'def test_pass(): assert True\n'
        'def test_base_failure(): assert False\n'
        '@pytest.mark.local_only\ndef test_machine(): assert False\n')
    (tests / 'test_locked.py').write_text('def test_locked(): assert False\n')
    plugin = Path(__file__).parents[1] / 'src/foragent/ci_pytest.py'
    (tmp_path / 'fa_measurement.py').write_bytes(plugin.read_bytes())
    monkeypatch.setenv('FA_SHARDS', '3')
    monkeypatch.setenv('PYTEST_DISABLE_PLUGIN_AUTOLOAD', '1')
    values = []
    for index in range(3):
        monkeypatch.setenv('FA_SHARD', str(index))
        values.append(measure(root, f'{sys.executable} -m pytest -o addopts= --rootdir=. -p no:cacheprovider',
                              'tests/acceptance', ['tests/acceptance/test_locked.py'], tmp_path, str(index)))
    assert all(value['complete'] for value in values)
    assert sorted(node for value in values for node in value['collected_ids']) == [
        'tests/acceptance/test_portable.py::test_base_failure', 'tests/acceptance/test_portable.py::test_pass']
    assert [node for value in values for node in value['passing_ids']] == ['tests/acceptance/test_portable.py::test_pass']


def test_force_added_exclusions_survive_generic_artifact_cleanup(git_history):
    from conftest import git_command
    from foragent.git import GitRepo

    root = git_history.clone
    git = GitRepo(root)
    metadata = root / '.foragent'
    metadata.mkdir()
    (metadata / 'acceptance-exclusions.json').write_text('[]\n')
    (metadata / 'TRY.md').write_text('scratch')
    git.publication_paths.add('.foragent/acceptance-exclusions.json')
    git.stage_changes()
    assert git_command(root, 'ls-files', '--', '.foragent') == '.foragent/acceptance-exclusions.json'
    git._exclude_artifacts()
    assert git_command(root, 'ls-files', '--', '.foragent') == '.foragent/acceptance-exclusions.json'


def test_fork_check_results_are_unusable_without_fetching_checks():
    from foragent.tracker import GhTracker, TrackerError

    value = report(count=1)
    value['run']['head_repository']['full_name'] = 'fork/repo'
    github, calls = recorded_github([value])
    with pytest.raises(TrackerError, match='foreign'):
        GhTracker('owner/repo', github).pr_acceptance_reports('head')
    assert len(calls) == 1


@pytest.mark.parametrize('checks', ['local', 'ci'])
def test_local_only_base_failures_are_ignored_but_head_unit_failures_are_not(git_history, tmp_path, monkeypatch, checks):
    from conftest import git_command
    from foragent.ci_local import capture_local_only, verify_local_only_units
    from foragent.git import GitRepo
    from foragent.runner import RunContext
    from foragent.state import StateStore
    from foragent.tracker import FakeTracker
    from datetime import UTC, datetime

    root = git_history.clone
    acceptance = root / 'tests/acceptance/test_machine.py'
    acceptance.write_text('import pytest\n'
        '@pytest.mark.local_only\ndef test_good(): assert True\n'
        '@pytest.mark.local_only\ndef test_bad_base(): assert False\n')
    unit = root / 'tests/test_machine.py'
    unit.write_text('import pytest\n@pytest.mark.local_only\ndef test_bad_unit(): assert False\n')
    store = StateStore(tmp_path / 'state')
    store.start_run(1, branch='test', worktree=root)
    config = Config(checks=checks, test_command=f'{sys.executable} -m pytest')
    ctx = RunContext('owner/repo', root, FakeTracker(), store, config, GitRepo(root), lambda: datetime.now(UTC))
    # Exercise a detached base via the existing no-commit history fixture.
    git_history.update({'tests/acceptance/test_machine.py': acceptance.read_text()})
    git_command(root, 'fetch', '-q', 'origin')
    baseline = capture_local_only(1, ctx, ctx.git, config.test_command, git_history.tip, [])
    assert baseline.node_ids == ('tests/acceptance/test_machine.py::test_good',)
    attempt = SimpleNamespace(n=1, ctx=ctx, worktree=root)
    result = verify_local_only_units(attempt, baseline.command)
    assert result.exit_code == 1


@pytest.mark.parametrize('checks', ['local', 'ci'])
@pytest.mark.parametrize('marking', ['absent', 'unmarked', 'decorator', 'module', 'hook', 'collection-error'])
def test_local_only_unit_check_requires_selected_tests(tmp_path, monkeypatch, checks, marking):
    from datetime import UTC, datetime
    from foragent.ci_local import check as real_check, verify_local_only_units
    from foragent.git import GitRepo
    from foragent.run_checks import CheckResult, _pytest_command
    from foragent.runner import RunContext
    from foragent.state import StateStore
    from foragent.tracker import FakeTracker

    root = tmp_path / 'repo'
    tests = root / 'tests'
    acceptance = tests / 'acceptance'
    acceptance.mkdir(parents=True)
    (acceptance / 'test_machine.py').write_text(
        'import pytest\n@pytest.mark.local_only\ndef test_acceptance(): assert False\n')
    if marking != 'absent':
        source = 'def test_unit(): assert False\n'
        if marking == 'decorator':
            source = 'import pytest\n@pytest.mark.local_only\n' + source
        elif marking == 'module':
            source = 'import pytest\npytestmark = pytest.mark.local_only\n' + source
        elif marking == 'hook':
            (root / 'conftest.py').write_text(
                'import pytest\ndef pytest_collection_modifyitems(items):\n'
                '    for item in items: item.add_marker(pytest.mark.local_only)\n')
        elif marking == 'collection-error':
            source = 'def test_unit(:\n'
        (tests / 'test_unit.py').write_text(source)
    store = StateStore(tmp_path / 'state')
    store.start_run(1, branch='test', worktree=root)
    config = Config(checks=checks, test_command=f'{sys.executable} -m pytest')
    ctx = RunContext('owner/repo', root, FakeTracker(), store, config, GitRepo(root), lambda: datetime.now(UTC))
    calls = []

    def check(n, ctx, command, worktree):
        if '--collect-only' in command:
            return real_check(n, ctx, command, worktree)
        calls.append(command)
        return CheckResult(command, 1, False, 'local unit failed')

    monkeypatch.setattr('foragent.ci_local.check', check)
    result = verify_local_only_units(SimpleNamespace(n=1, ctx=ctx, worktree=root),
                                     _pytest_command(config.test_command))
    assert len(calls) == int(marking in {'decorator', 'module', 'hook'})
    if marking in {'absent', 'unmarked', 'collection-error'}:
        assert result is None
    else:
        assert result.exit_code == 1


def test_tracker_reads_every_page_of_head_check_runs():
    from foragent.tracker import GhTracker

    github, calls = recorded_github([report(), report(1)])
    tracker = GhTracker('owner/repo', github)
    assert tracker.pr_default_branch() == 'trunk'
    assert compare(tracker.pr_acceptance_reports('head')) == ([], 3)
    assert len(calls) == 5
    assert all('--paginate' in args for args in calls if '--jq' in args)
    assert all('/check-runs' not in args[-1] for args in calls)


def test_local_mode_generated_workflow_does_not_require_ci_inputs(monkeypatch, tmp_path, capsys):
    from foragent.ci_shard import main

    monkeypatch.chdir(tmp_path)
    (tmp_path / '.foragent.yml').write_text('checks: local\n')
    main()
    assert 'locally' in capsys.readouterr().out


def test_shard_without_head_exclusions_succeeds_without_a_report(tmp_path, monkeypatch):
    from foragent import ci_shard
    from foragent.ci_workflow import EXCLUSIONS

    artifact = tmp_path / 'shard-0.json'
    output = tmp_path / 'github-output'
    output.write_text('existing=value\n')
    for key, value in {'FA_CHECKOUT': str(tmp_path), 'FA_BASE_SHA': 'base', 'FA_HEAD_SHA': 'head',
                       'FA_REPORT_FILE': str(artifact), 'GITHUB_OUTPUT': str(output)}.items():
        monkeypatch.setenv(key, value)
    assert not (tmp_path / EXCLUSIONS).exists()
    calls = []

    def git(args, **kwargs):
        calls.append(args)
        assert kwargs['cwd'] == tmp_path
        assert args == ['git', 'rev-parse', 'HEAD'] or args[1:3] in (['worktree', 'add'], ['worktree', 'remove'])
        return SimpleNamespace(stdout='head\n')

    monkeypatch.setattr(ci_shard.subprocess, 'run', git)
    monkeypatch.setattr(ci_shard, 'load_config', lambda *args, **kwargs: Config(checks='ci', test_command='pytest'))
    monkeypatch.setattr(ci_shard, 'measure', lambda *args: pytest.fail('non-CI-mode head was measured'))
    assert ci_shard.main() is None
    assert calls == [['git', 'rev-parse', 'HEAD']]
    assert not artifact.exists()
    assert output.read_text() == 'existing=value\n'


@pytest.mark.parametrize('fallback', [None, 'local'])
def test_ci_head_without_exclusions_and_reports_holds_or_falls_back(tmp_path, monkeypatch, fallback):
    from conftest import BASE_TIME
    from foragent import ci_mode, ci_local
    from foragent.ci_workflow import EXCLUSIONS
    from foragent.reasons import Reason
    from foragent.run_attempt import ExecutorStopped

    config = Config(checks='ci', checks_fallback=fallback, test_command='pytest')
    updates, flags, reads, local = [], [], [], []
    clock = [BASE_TIME]

    def sleep(seconds):
        clock[0] += timedelta(seconds=seconds)
    ctx = SimpleNamespace(
        repo='owner/repo', repo_root=tmp_path, config=config, now=lambda: clock[0], sleep=sleep,
        tracker=SimpleNamespace(pr_default_branch=lambda: 'main', pr_acceptance_reports=lambda pr: reads.append(pr) or []),
        store=SimpleNamespace(update_run=lambda n, **kwargs: updates.append(kwargs)),
    )
    attempt = SimpleNamespace(
        n=1, ctx=ctx, worktree=tmp_path, target_branch='main', log=tmp_path / 'log',
        git=SimpleNamespace(ref_sha=lambda ref: 'trusted', _call=lambda args: ''),
    )
    assert not (tmp_path / EXCLUSIONS).exists()
    monkeypatch.setattr(ci_mode, '_baseline_exclusions', lambda *args: [])
    monkeypatch.setattr(ci_mode, 'flag', lambda *args: flags.append(args))
    monkeypatch.setattr(ci_local, 'fallback_comparison', lambda attempt, base: local.append(base))
    monkeypatch.setattr('foragent.runner._remaining', lambda *args: 3600)
    if fallback is None:
        with pytest.raises(ExecutorStopped, match='missing acceptance shards') as stopped:
            ci_mode.require_comparison(attempt, 2, 'head', base_sha='base')
        assert stopped.value.outcome == 'held'
        assert flags[0][2] == Reason.ACCEPTANCE_BASELINE_UNAVAILABLE
        assert not local
    else:
        evidence = ci_mode.require_comparison(attempt, 2, 'head', base_sha='base')
        assert evidence.reports is None and local == ['base']
        assert not flags
    assert reads
    assert not any(update.get('comparison', {}).get('source') == 'ci' for update in updates)


def test_large_comparisons_fit_github_output_without_losing_any_ids():
    from foragent.tracker import GhTracker

    value = report(count=1)
    payload = value['report']
    ids = [f'tests/acceptance/test_large.py::TestRegression::test_case[parameter-{index}]' for index in range(1500)]
    for side in ('base', 'head'):
        payload[side] = {'complete': True, 'collected_ids': ids, 'passing_ids': ids}
    payload['head']['passing_ids'] = ids[:-1]
    assert len(json.dumps(payload).encode()) > 65535
    github, _ = recorded_github([value])
    assert compare(GhTracker('owner/repo', github).pr_acceptance_reports('head'), count=1)[0] == [ids[-1]]


def test_damaged_compressed_report_is_unusable():
    from foragent.tracker import GhTracker, TrackerError

    value = report(count=1)
    github, _ = recorded_github([value])
    output = BytesIO()
    with ZipFile(output, 'w') as archive:
        archive.writestr('shard-0.json', json.dumps({'encoding': 'zlib+base64', 'excluded': [], 'data': 'eA=='}))

    def damaged(args, input=None):
        return output.getvalue() if args[-1].endswith('/zip') else github(args, input)

    with pytest.raises(TrackerError, match='Unusable acceptance artifacts'):
        GhTracker('owner/repo', damaged).pr_acceptance_reports('head')


def test_shard_publishes_real_base_and_head_measurements(git_history, tmp_path, monkeypatch):
    from conftest import git_command
    from foragent import ci_shard
    from foragent.ci_transport import decode_report
    import tempfile

    config = yaml.safe_dump({'checks': 'ci', 'ci_shards': 1, 'test_command': f'{sys.executable} -m pytest'})
    keep = 'tests/acceptance/test_keep.py'
    git_history.update({'.foragent.yml': config, '.foragent/acceptance-exclusions.json': '[]\n',
                        keep: 'def test_keep(): assert True\n'})
    base = git_history.tip
    git_history.update({keep: 'def test_keep(): assert False\n'})
    head = git_history.tip
    git_command(git_history.clone, 'fetch', '-q', 'origin')
    git_command(git_history.clone, 'merge', '-q', '--ff-only', 'origin/main')
    monkeypatch.chdir(git_history.clone)
    monkeypatch.setattr(tempfile, 'tempdir', str(tmp_path))
    output = tmp_path / 'output'
    for key, value in {'FA_BASE_SHA': base, 'FA_HEAD_SHA': head, 'FA_REPOSITORY': 'owner/repo',
                       'FA_SHARD': '0', 'FA_SHARDS': '1', 'GH_TOKEN': 'fake-token',
                       'FA_REPORT_FILE': str(tmp_path / 'shard-0.json'),
                       'GITHUB_OUTPUT': str(output), 'PYTEST_DISABLE_PLUGIN_AUTOLOAD': '1'}.items():
        monkeypatch.setenv(key, value)
    ci_shard.main()
    payload = json.loads((tmp_path / 'shard-0.json').read_text())
    assert payload['base_sha'] == base and payload['head_sha'] == head
    assert payload['base']['complete'] and payload['head']['complete']
    assert f'{keep}::test_keep' in payload['base']['passing_ids']
    assert f'{keep}::test_keep' not in payload['head']['passing_ids']
    assert output.read_text() == 'comparison=true\n'


@pytest.mark.parametrize('side', ['base', 'head'])
@pytest.mark.parametrize('staged', [False, True], ids=['worktree', 'index'])
def test_shard_rejects_measurement_that_rewrites_tracked_source(git_history, tmp_path, monkeypatch, side, staged):
    from conftest import git_command
    from foragent import ci_shard
    from foragent.ci_transport import decode_report

    config = Config(checks='ci', ci_shards=1, test_command=f'{sys.executable} -m pytest')
    keep = 'tests/acceptance/test_keep.py::test_keep'
    test = ('from pathlib import Path\nimport subprocess\n'
            'def test_keep():\n'
            '    source = Path("src/demo.py")\n'
            '    source.write_text("VALUE = 1\\n")\n')
    if staged:
        test += '    subprocess.run(["git", "add", "src/demo.py"], check=True)\n'
    test += '    assert source.read_text() == "VALUE = 1\\n"\n'
    git_history.update({'.foragent.yml': yaml.safe_dump({'checks': 'ci', 'ci_shards': 1,
                        'test_command': config.test_command}), '.foragent/acceptance-exclusions.json': '[]\n',
                        'src/demo.py': f'VALUE = {2 if side == "base" else 1}\n', keep.split('::')[0]: test})
    base = git_history.tip
    git_history.update({'src/demo.py': f'VALUE = {2 if side == "head" else 1}\n'})
    head = git_history.tip
    git_command(git_history.clone, 'fetch', '-q', 'origin')
    git_command(git_history.clone, 'merge', '-q', '--ff-only', 'origin/main')
    artifact = tmp_path / 'shard-0.json'
    for name, value in {'FA_CHECKOUT': str(git_history.clone), 'FA_BASE_SHA': base, 'FA_HEAD_SHA': head,
                        'FA_REPOSITORY': 'owner/repo', 'FA_SHARD': '0', 'FA_SHARDS': '1',
                        'FA_REPORT_FILE': str(artifact), 'GITHUB_OUTPUT': str(tmp_path / 'outputs'),
                        'PYTEST_DISABLE_PLUGIN_AUTOLOAD': '1'}.items():
        monkeypatch.setenv(name, value)

    ci_shard.main()
    payload = decode_report(artifact.read_text())
    assert all(keep in payload[name]['passing_ids'] for name in ('base', 'head'))
    assert payload[side]['complete'] is False
    assert payload['head' if side == 'base' else 'base']['complete'] is True
    check = report(count=1)
    check['report'] = json.loads(artifact.read_text())
    check['run']['head_sha'] = check['artifact']['workflow_run']['head_sha'] = head
    with pytest.raises(UnusableComparison, match='incomplete suite'):
        comparison([check], 'owner/repo', base, head, [],
                   expected_scope=measurement_scope(config, git_history.clone, []), expected_shards=[0])


@pytest.mark.parametrize('max_retries', [0, 1])
def test_local_fallback_cannot_merge_the_head_before_a_tracked_rewrite(run_case, monkeypatch, max_retries):
    from conftest import git_command
    from foragent.runner import run_issue

    case = run_case
    case.ctx.config = replace(case.ctx.config, checks='ci', checks_fallback='local', max_retries=max_retries,
                              test_command=f'{sys.executable} -m pytest')
    case.history.update({
        'src/demo.py': 'VALUE = 1\n',
        'tests/acceptance/test_keep.py': 'from pathlib import Path\ndef test_keep():\n'
            '    source = Path("src/demo.py")\n'
            '    source.write_text("VALUE = 1\\n")\n'
            '    assert source.read_text() == "VALUE = 1\\n"\n',
    })
    case.executor.steps = [{'feature.txt': 'done', 'src/demo.py': 'VALUE = 2\n'}, {'feature.txt': 'done'}]
    monkeypatch.setattr(case.tracker, 'pr_acceptance_reports', lambda pr: [])

    outcome = run_issue(1, case.ctx)
    if max_retries:
        assert outcome == 'merged'
        head = case.tracker.pr_head_sha(2)
        assert git_command(case.history.origin, 'show', f'{head}:src/demo.py') == 'VALUE = 1'
        assert len(case.executor.calls) == 2
        assert 'Acceptance baseline verification count=1' in case.store.log_path(1).read_text()
        assert case.store.get_run(1).comparison['source'] == 'local'
    else:
        assert outcome != 'merged'
        assert not case.tracker.merged_prs
        assert 'modified tracked files' in case.store.log_path(1).read_text()


@pytest.mark.parametrize('local_only', [False, True], ids=['fallback', 'local-only'])
@pytest.mark.parametrize('staged', [False, True], ids=['worktree', 'index'])
def test_local_baseline_rejects_measurement_that_rewrites_tracked_source(
    git_history, tmp_path, local_only, staged,
):
    from conftest import BASE_TIME, git_command
    from foragent.ci_local import LocalComparisonUnavailable, capture_local_only, fallback_comparison
    from foragent.git import GitRepo
    from foragent.run_attempt import AttemptFailure
    from foragent.run_baseline import AcceptanceBaseline
    from foragent.runner import RunContext
    from foragent.state import StateStore
    from foragent.tracker import FakeTracker

    test = ('from pathlib import Path\nimport subprocess\nimport pytest\n'
            + ('@pytest.mark.local_only\n' if local_only else '')
            + 'def test_keep():\n'
            '    source = Path("src/demo.py")\n'
            '    source.write_text("VALUE = 1\\n")\n')
    if staged:
        test += '    subprocess.run(["git", "add", "src/demo.py"], check=True)\n'
    test += '    assert source.read_text() == "VALUE = 1\\n"\n'
    git_history.update({'src/demo.py': 'VALUE = 2\n', 'tests/acceptance/test_keep.py': test})
    base_sha = git_history.tip
    git_history.update({'src/demo.py': 'VALUE = 1\n'})
    root = git_history.clone
    git_command(root, 'fetch', '-q', 'origin')
    git_command(root, 'merge', '-q', '--ff-only', 'origin/main')
    store = StateStore(tmp_path / 'state', now=lambda: BASE_TIME)
    store.start_run(1, branch='test', worktree=root)
    config = Config(checks='ci', test_command=f'{sys.executable} -m pytest')
    ctx = RunContext('owner/repo', root, FakeTracker(), store, config, GitRepo(root), lambda: BASE_TIME)
    # Only the base is rewritten: the head already has the test's generated value.
    attempt = SimpleNamespace(n=1, ctx=ctx, git=ctx.git, worktree=root, target_branch='main',
                              baseline=AcceptanceBaseline(config.test_command, ()))

    for _ in range(2):
        error = AttemptFailure if local_only else LocalComparisonUnavailable
        with pytest.raises(error, match='modified tracked files'):
            if local_only:
                capture_local_only(1, ctx, ctx.git, config.test_command, base_sha, [])
            else:
                fallback_comparison(attempt, base_sha)
        assert not list((store.root.parent / 'baselines').glob('*.json'))
        assert not ctx.git._call(['status', '--porcelain=v1', '--untracked-files=no'])


@pytest.mark.parametrize('runner_unit_tests', ['all', 'ci', 'auto'])
def test_review_checks_unit_selection_honors_runner_unit_tests(git_history, tmp_path, runner_unit_tests):
    from conftest import BASE_TIME, git_command
    from foragent.ci_mode import review_checks
    from foragent.git import GitRepo
    from foragent.run_attempt import AttemptFailure
    from foragent.runner import RunContext
    from foragent.state import StateStore
    from foragent.tracker import FakeTracker

    git_history.update({
        'src/demo.py': 'VALUE = 1\n',
        'tests/conftest.py': 'import sys\nfrom pathlib import Path\n'
            'sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))\n',
        'tests/test_demo.py': 'from demo import VALUE\ndef test_demo(): assert VALUE > 0\n',
        'tests/test_importer.py': 'from demo import VALUE\ndef test_importer(): assert VALUE == 1\n',
    })
    root = git_history.clone
    git_command(root, 'fetch', '-q', 'origin')
    git_command(root, 'merge', '-q', '--ff-only', 'origin/main')
    (root / 'src/demo.py').write_text('VALUE = 2\n')
    git = GitRepo(root)
    git.stage_changes()
    store = StateStore(tmp_path / 'state', now=lambda: BASE_TIME)
    store.start_run(1, branch='test', worktree=root)
    config = Config(checks='ci', runner_unit_tests=runner_unit_tests, test_command=f'{sys.executable} -m pytest')
    ctx = RunContext('owner/repo', root, FakeTracker(), store, config, git, lambda: BASE_TIME)

    if runner_unit_tests == 'all':
        with pytest.raises(AttemptFailure, match='test_importer'):
            review_checks(1, ctx, git, 'main')
    else:
        assert review_checks(1, ctx, git, 'main').git is git
        assert 'test_importer' not in store.log_path(1).read_text()
    assert 'tests/test_demo.py' in store.log_path(1).read_text()


def test_workflow_only_measures_issue_prs_to_the_configured_primary_base():
    document = yaml.safe_load(workflow_text(Config(base_branch='release')))
    condition = document['jobs']['acceptance']['if']
    assert 'github.event.pull_request.head.repo.full_name == github.repository' in condition
    assert "startsWith(github.event.pull_request.head.ref, 'foragent/issue-')" in condition
    assert "github.event.pull_request.base.ref == 'release'" in condition


@pytest.mark.parametrize('damage', ['foreign-run', 'missing-run', 'wrong-workflow', 'wrong-repository',
                                  'wrong-event', 'mixed-runs', 'wrong-sha', 'wrong-name', 'expired', 'duplicate-id'])
def test_unrelated_producers_cannot_supply_acceptance_evidence(damage):
    checks = [report(), report(1)]
    for check in checks:
        if damage == 'foreign-run':
            check['artifact']['workflow_run']['id'] = 999
        elif damage == 'missing-run':
            check.pop('run')
        elif damage == 'wrong-workflow':
            check['run']['path'] = '.github/workflows/unrelated.yml'
        elif damage == 'wrong-repository':
            check['run']['repository']['full_name'] = 'owner/other'
        elif damage == 'wrong-event':
            check['run']['event'] = 'workflow_run'
        elif damage == 'mixed-runs' and check['report']['shard_index'] == 1:
            check['run']['id'] = check['artifact']['workflow_run']['id'] = 43
        elif damage == 'wrong-sha':
            check['run']['head_sha'] = 'old'
        elif damage == 'wrong-name':
            check['artifact']['name'] += '-edited'
        elif damage == 'expired':
            check['artifact']['expired'] = True
        elif damage == 'duplicate-id':
            check['artifact']['id'] = 100
    with pytest.raises(UnusableComparison):
        compare(checks)


def test_measurement_cannot_read_publication_credentials(tmp_path, monkeypatch):
    from foragent.ci_shard import measure

    root = tmp_path / 'repo'
    acceptance = root / 'tests/acceptance'
    acceptance.mkdir(parents=True)
    secret_names = ['GH_TOKEN', 'GITHUB_TOKEN', 'GH_ENTERPRISE_TOKEN', 'GITHUB_ENTERPRISE_TOKEN',
                    'ACTIONS_RUNTIME_TOKEN', 'ACTIONS_ID_TOKEN_REQUEST_TOKEN']
    for name in secret_names:
        monkeypatch.setenv(name, 'sentinel-checks-write-token')
    (acceptance / 'test_credentials.py').write_text(
        f'import os\ndef test_credentials():\n    assert not set({secret_names!r}) & os.environ.keys()\n')
    (tmp_path / 'fa_measurement.py').write_bytes(
        (Path(__file__).parents[1] / 'src/foragent/ci_pytest.py').read_bytes())
    monkeypatch.setenv('FA_SHARDS', '1')
    monkeypatch.setenv('FA_SHARD', '0')
    monkeypatch.setenv('PYTEST_DISABLE_PLUGIN_AUTOLOAD', '1')
    value = measure(root, f'{sys.executable} -m pytest --rootdir=. -p no:cacheprovider',
                    'tests/acceptance', [], tmp_path, 'head')
    assert value['complete'] and value['passing_ids'] == ['tests/acceptance/test_credentials.py::test_credentials']
    workflow = yaml.safe_load(workflow_text(Config()))
    measurement = workflow['jobs']['acceptance']
    assert measurement['permissions'] == {'contents': 'read'}
    assert measurement['steps'][0]['with']['persist-credentials'] is False
    step = measurement['steps'][2]
    assert step['working-directory'] == '${{ runner.temp }}'
    assert not set(secret_names) & step['env'].keys()
    assert 'PYTHONPATH' not in step['env']
    assert workflow['permissions'] == {'contents': 'read'}


def test_head_configuration_cannot_erase_baseline_scope(git_history, tmp_path, monkeypatch):
    from conftest import git_command
    from foragent import ci_shard
    from foragent.ci_transport import decode_report

    config = Config(checks='ci', ci_shards=1, test_command=f'{sys.executable} -m pytest')
    keep = 'tests/acceptance/test_keep.py::test_keep'
    git_history.update({'.foragent.yml': yaml.safe_dump({'checks': 'ci', 'ci_shards': 1,
                       'test_command': config.test_command}), '.foragent/acceptance-exclusions.json': '[]',
                       keep.split('::')[0]: 'def test_keep(): assert True\n'})
    base = git_history.tip
    git_history.update({'.foragent.yml': 'checks: local\nacceptance_dir: nonexistent\ntest_command: npm test\nci_shards: 1\n',
                       keep.split('::')[0]: 'def test_keep(): assert False\n'})
    head = git_history.tip
    git_command(git_history.clone, 'fetch', '-q', 'origin')
    git_command(git_history.clone, 'merge', '-q', '--ff-only', 'origin/main')
    monkeypatch.chdir(git_history.clone)
    artifact = tmp_path / 'shard-0.json'
    for name, value in {'FA_BASE_SHA': base, 'FA_HEAD_SHA': head, 'FA_REPOSITORY': 'owner/repo',
                        'FA_SHARD': '0', 'FA_SHARDS': '1', 'FA_REPORT_FILE': str(artifact),
                        'GITHUB_OUTPUT': str(tmp_path / 'outputs'), 'PYTEST_DISABLE_PLUGIN_AUTOLOAD': '1'}.items():
        monkeypatch.setenv(name, value)
    ci_shard.main()
    payload = decode_report(artifact.read_text())
    scope = measurement_scope(config, git_history.clone, [])
    assert payload['scope'] == scope
    assert keep in payload['base']['passing_ids'] and keep not in payload['head']['passing_ids']
    check = report(count=1)
    check['report'] = json.loads(artifact.read_text())
    check['run']['head_sha'] = check['artifact']['workflow_run']['head_sha'] = head
    assert comparison([check], 'owner/repo', base, head, [], expected_scope=scope, expected_shards=[0])[0] == [keep]
    payload['scope']['acceptance_dir'] = 'nonexistent'
    for side in ('base', 'head'):
        payload[side] = {'complete': True, 'collected_ids': [], 'passing_ids': []}
    check['report'] = payload
    with pytest.raises(UnusableComparison):
        comparison([check], 'owner/repo', base, head, [], expected_scope=scope, expected_shards=[0])


def test_report_cannot_shrink_the_expected_four_shard_layout():
    check = report(count=1)
    payload = check['report']
    payload['scope'] = measurement_scope(Config(ci_shards=4), Path('.'), [])
    check['report'] = payload
    with pytest.raises(UnusableComparison):
        compare([check], count=4)


@pytest.mark.parametrize('command', ['npm test', 'go test ./...'])
def test_non_pytest_ci_uses_local_comparison_and_invokes_worker(run_case, command):
    from foragent.runner import run_issue

    case = run_case
    case.ctx.config = replace(case.ctx.config, checks='ci', test_command=command)
    assert run_issue(1, case.ctx) == 'merged'
    assert len(case.executor.calls) == 1
    assert case.store.get_run(1).comparison['source'] == 'local'


@pytest.mark.parametrize('damage', [None, 'no-shards', 'foreign-repository', 'wrong-workflow', 'wrong-event',
                                  'foreign-head', 'missing-shard', 'wrong-shard'])
def test_publisher_accepts_only_complete_artifacts_from_the_triggering_run(tmp_path, monkeypatch, damage):
    """The runner now validates measurement artifacts directly (legacy test name)."""
    from foragent.tracker import GhTracker, TrackerError

    values = [report(), report(1)]
    runs = [deepcopy(values[0]['run'])]
    if damage == 'foreign-repository':
        runs[0]['head_repository']['full_name'] = 'fork/repo'
    elif damage == 'wrong-workflow':
        runs[0]['path'] = '.github/workflows/other.yml'
    elif damage == 'wrong-event':
        runs[0]['event'] = 'push'
    elif damage == 'foreign-head':
        runs[0]['head_sha'] = 'other-head'
    elif damage == 'no-shards':
        values = []
    elif damage == 'missing-shard':
        values.pop()
    elif damage == 'wrong-shard':
        values[1]['report']['shard_index'] = 0
    github, calls = recorded_github(values, runs=runs)
    tracker = GhTracker('owner/repo', github)
    if damage:
        with pytest.raises((TrackerError, UnusableComparison)):
            compare(tracker.pr_acceptance_reports('head'))
    else:
        assert compare(tracker.pr_acceptance_reports('head')) == ([], 3)
    assert all('/check-runs' not in args[-1] for args in calls)


@pytest.mark.parametrize('settings', [{'checks': 'local', 'test_command': 'uv run pytest'},
                                    {'checks': 'ci', 'test_command': 'npm test'}])
def test_base_local_or_non_pytest_configuration_skips_ci_measurement(git_history, tmp_path, monkeypatch, settings):
    from conftest import git_command
    from foragent import ci_shard

    git_history.update({'.foragent.yml': yaml.safe_dump(settings)})
    base = git_history.tip
    git_history.update({'.foragent.yml': 'checks: ci\ntest_command: uv run pytest\n'})
    git_command(git_history.clone, 'fetch', '-q', 'origin')
    git_command(git_history.clone, 'merge', '-q', '--ff-only', 'origin/main')
    monkeypatch.setenv('FA_CHECKOUT', str(git_history.clone))
    monkeypatch.setenv('FA_BASE_SHA', base)
    monkeypatch.setenv('FA_HEAD_SHA', git_history.tip)
    artifact = tmp_path / 'unexpected-report.json'
    monkeypatch.setenv('FA_REPORT_FILE', str(artifact))
    monkeypatch.setattr(ci_shard, 'measure', lambda *args: pytest.fail('local mode must not execute CI measurements'))
    ci_shard.main()
    assert not artifact.exists()


@pytest.mark.parametrize('workflow', [WORKFLOW, '.github/workflows/foragent-acceptance-extra.yml'])
@pytest.mark.parametrize('regressed', [False, True])
def test_changed_acceptance_workflow_requires_local_comparison_even_with_fabricated_shards(
    run_case, monkeypatch, workflow, regressed,
):
    from foragent.runner import run_issue

    case = run_case
    case.ctx.config = replace(case.ctx.config, checks='ci', checks_fallback=None, max_retries=0, ci_shards=2,
                              test_command=f'{sys.executable} -m pytest')
    case.history.update({WORKFLOW: 'name: measurement\non: [pull_request]\n',
                         'src/__init__.py': '', 'src/demo.py': 'VALUE = 1\n',
                         'tests/acceptance/test_keep.py':
                             'from src.demo import VALUE\ndef test_keep(): assert VALUE == 1\n'})
    case.executor.steps = [{'feature.txt': 'done', workflow: 'name: fabricated uploads\non: [pull_request]\n',
                            'src/demo.py': f'VALUE = {2 if regressed else 1}\n'}]
    forged = [report(), report(1)]
    for check in forged:
        payload = check['report']
        for side in ('base', 'head'):
            payload[side] = {'complete': True, 'collected_ids': [], 'passing_ids': []}
        check['report'] = payload
    reads = []

    def forged_checks(head):
        from foragent.ci_workflow import EXCLUSIONS

        reads.append(head)
        excluded = json.loads((case.store.worktree_path(1) / EXCLUSIONS).read_text())
        for check in forged:
            check['run']['head_sha'] = check['artifact']['workflow_run']['head_sha'] = head
            payload = check['report']
            payload.update(base_sha=case.history.tip, head_sha=head, excluded=excluded,
                           scope=measurement_scope(case.ctx.config, case.ctx.repo_root, excluded))
            check['report'] = payload
        return forged

    monkeypatch.setattr(case.tracker, 'pr_acceptance_reports', forged_checks)

    outcome = run_issue(1, case.ctx)
    assert (outcome == 'merged') is not regressed
    assert not reads, 'A changed workflow must never supply CI evidence for this head'
    assert len(case.executor.calls) == 1
    if not regressed:
        assert case.store.get_run(1).comparison['source'] == 'local'
    else:
        assert 'test_keep' in case.store.log_path(1).read_text()


@pytest.mark.parametrize('damage', ['unrelated-workflow', 'foreign-run', 'failed-newer-run', None])
def test_tracker_binds_borrowed_publisher_url_to_github_suite_and_run_ids(damage):
    """URLs/check suites cannot substitute for the run/artifact relationship."""
    from foragent.tracker import GhTracker, TrackerError

    value = report(count=1)
    run = deepcopy(value['run'])
    runs = [run]
    if damage == 'unrelated-workflow':
        run['path'] = '.github/workflows/unrelated.yml'
    elif damage == 'foreign-run':
        value['artifact']['workflow_run']['id'] = 99
    elif damage == 'failed-newer-run':
        runs.append({**run, 'id': 99, 'conclusion': 'failure'})
    github, calls = recorded_github([value], runs=runs)
    tracker = GhTracker('owner/repo', github)
    if damage:
        with pytest.raises((UnusableComparison, TrackerError)):
            compare(tracker.pr_acceptance_reports('head'), count=1)
    else:
        assert compare(tracker.pr_acceptance_reports('head'), count=1)[0] == []
    assert all('/check-runs' not in args[-1] for args in calls)


def test_local_only_baseline_rejects_exit_after_preexisting_failure_before_remaining_test(run_case):
    from foragent.runner import run_issue

    case = run_case
    case.ctx.config = replace(case.ctx.config, checks='ci', test_command=f'{sys.executable} -m pytest')
    case.history.update({
        'src/__init__.py': '', 'src/demo.py': 'VALUE = 1\n',
        'tests/acceptance/test_machine.py':
            'import pytest\nfrom src.demo import VALUE\npytestmark = pytest.mark.local_only\n'
            'def test_preexisting_failure(): assert False\n'
            'def test_remaining(): assert VALUE == 1\n',
        'conftest.py':
            'import pytest\n@pytest.hookimpl(hookwrapper=True)\n'
            'def pytest_runtest_protocol(item, nextitem):\n    yield\n'
            '    if item.name == "test_preexisting_failure": pytest.exit("stopped early", returncode=1)\n',
    })
    case.executor.steps = [{'feature.txt': 'done', 'src/demo.py': 'VALUE = 2\n'}]
    assert run_issue(1, case.ctx) == 'acceptance-baseline-unavailable'
    assert not case.executor.calls
    assert 'incomplete suite' in case.store.log_path(1).read_text()


def test_local_mode_runs_machine_only_importer_when_ci_owns_portable_units(run_case):
    from foragent.runner import run_issue

    case = run_case
    case.ctx.config = replace(case.ctx.config, checks='local', max_retries=0, test_command=f'{sys.executable} -m pytest')
    case.history.update({
        '.github/workflows/ci.yml': 'name: ci\non: [pull_request]\njobs: {}\n',
        'src/__init__.py': '', 'src/demo.py': 'VALUE = 1\n',
        'tests/test_demo.py': 'from src.demo import VALUE\ndef test_demo(): assert VALUE > 0\n',
        'tests/test_importer.py': 'import pytest\nfrom src.demo import VALUE\n'
            '@pytest.mark.local_only\ndef test_importer(): assert VALUE == 1\n',
    })
    case.executor.steps = [{'feature.txt': 'done', 'src/demo.py': 'VALUE = 2\n'}]
    assert run_issue(1, case.ctx) != 'merged'
    assert len(case.executor.calls) == 1
    assert 'test_importer' in case.store.log_path(1).read_text()
    assert not case.tracker.merged_prs


def test_undetected_test_command_uses_local_path_and_invokes_worker(run_case):
    from foragent.ci_workflow import acceptance_command, initial_checks
    from foragent.runner import run_issue

    case = run_case
    case.ctx.config = replace(case.ctx.config, checks='ci')
    assert acceptance_command(case.ctx.config, case.history.clone) is None
    assert initial_checks('owner/repo', lambda *args, **kwargs: '{"isPrivate": false}') == 'local'
    assert run_issue(1, case.ctx) == 'merged'
    assert len(case.executor.calls) == 1
    assert case.store.get_run(1).comparison['source'] == 'local'


@pytest.mark.parametrize('reviewed', [False, True], ids=['implementation', 'review-only'])
@pytest.mark.parametrize('change', ['producer', 'regression', 'missing', 'base', 'head', 'scope', 'infrastructure'])
def test_final_lock_rechecks_comparison_and_releases_before_reverification(run_case, monkeypatch, reviewed, change):
    from foragent import runner
    from foragent.ci_workflow import EXCLUSIONS
    from conftest import git_command

    case = run_case
    case.ctx.config = replace(case.ctx.config, checks='ci', max_retries=0, ci_shards=2,
                              test_command=f'{sys.executable} -m pytest', review='careful' if reviewed else 'fast')
    case.tracker.set_pr_checks(2, 'pass')
    if reviewed:
        assert runner.run_issue(1, case.ctx) == 'pr-open'
        case.now[0] += timedelta(seconds=1)
        case.ctx.config = replace(case.ctx.config, review='fast')
    if change == 'infrastructure':
        from foragent.tracker import CIEvidence

        case.ctx.config = replace(case.ctx.config, checks_fallback='local')
        case.tracker.set_pr_checks(2, 'fail')
        case.tracker.set_pr_failed_logs(2, CIEvidence('Job did not start', not_started=True))

    holder = [False]
    changed = [False]
    reads = []
    heads = set()
    locks = []
    merge_lock = case.store.merge_lock
    sleep = case.ctx.sleep

    def checks(head):
        reads.append(holder[0])
        heads.add(head)
        if changed[0] and change == 'missing':
            return []
        excluded = json.loads((case.store.worktree_path(1) / EXCLUSIONS).read_text())
        values = [report(), report(1)]
        for value in values:
            value['run']['head_sha'] = value['artifact']['workflow_run']['head_sha'] = head
            if changed[0] and change == 'producer':
                value['run']['id'] = value['artifact']['workflow_run']['id'] = 43
            payload = value['report']
            payload.update(base_sha=case.history.tip, head_sha=head, excluded=excluded,
                           scope=measurement_scope(case.ctx.config, case.ctx.repo_root, excluded))
            if changed[0] and change == 'regression':
                payload['head']['passing_ids'] = []
            value['report'] = payload
        return values

    @contextmanager
    def observed_lock():
        if not changed[0]:
            changed[0] = True
            if change == 'base':
                case.history.update({'docs/new.md': 'Another merge advanced main\n'})
            elif change == 'head':
                case.tracker.set_pr_head_sha(2, 'a' * 40)
            elif change == 'scope':
                case.ctx.config = replace(case.ctx.config, ci_shards=3)
            elif change == 'infrastructure':
                case.tracker.set_pr_failed_logs(2, CIEvidence('A rerun failed a code check', not_started=False))
        with merge_lock():
            holder[0] = True
            locks.append(len(reads))
            try:
                yield
            finally:
                holder[0] = False

    def unlocked_sleep(seconds):
        assert not holder[0], 'Comparison polling must release the final merge lock'
        sleep(seconds)

    monkeypatch.setattr(case.tracker, 'pr_acceptance_reports', checks)
    monkeypatch.setattr(case.store, 'merge_lock', observed_lock)
    case.ctx.sleep = unlocked_sleep
    outcome = runner.review_only(1, case.ctx, repair_ci=False) if reviewed else runner.run_issue(1, case.ctx)

    if change == 'producer' or (change == 'base' and not reviewed):
        assert outcome == 'merged'
        assert len(locks) == 2
        assert len(heads) == (2 if change == 'base' else 1)
    else:
        assert outcome != 'merged' and not case.tracker.merged_prs
    if change in {'producer', 'regression', 'missing'}:
        assert reads[:3] == [False, True, False], 'Changed evidence must be checked again outside the lock'
    if change == 'base' and reviewed:
        assert outcome == 'base-changed'
    if change == 'head':
        assert outcome == 'held'
    assert git_command(case.history.origin, 'rev-parse', 'main') == case.history.tip


@pytest.mark.parametrize('damage', ['not-zip', 'wrong-member', 'duplicate-member', 'truncated-json',
                                  'expired', 'foreign-run', 'duplicate-name', 'duplicate-id'])
def test_tracker_rejects_damaged_or_ambiguous_artifacts(damage):
    from foragent.tracker import GhTracker, TrackerError

    values = [report(), report(1)]
    if damage == 'expired':
        values[1]['artifact']['expired'] = True
    elif damage == 'foreign-run':
        values[1]['artifact']['workflow_run']['id'] = 99
    elif damage == 'duplicate-name':
        values[1]['artifact']['name'] = 'acceptance-0'
    elif damage == 'duplicate-id':
        values[1]['artifact']['id'] = values[0]['artifact']['id']
    github, _ = recorded_github(values)

    def damaged(args, input=None):
        if args[-1].endswith('/zip') and damage in {'not-zip', 'wrong-member', 'duplicate-member', 'truncated-json'}:
            if damage == 'not-zip':
                return b'not a ZIP archive'
            output = BytesIO()
            with ZipFile(output, 'w') as archive:
                filename = 'shard-9.json' if damage == 'wrong-member' else 'shard-0.json'
                archive.writestr(filename, '{' if damage == 'truncated-json' else json.dumps(values[0]['report']))
                if damage == 'duplicate-member':
                    with pytest.warns(UserWarning, match='Duplicate name'):
                        archive.writestr(filename, json.dumps(values[0]['report']))
            return output.getvalue()
        return github(args, input)

    with pytest.raises(TrackerError, match='Unusable acceptance artifacts'):
        GhTracker('owner/repo', damaged).pr_acceptance_reports('head')


def test_trusted_workflow_revision_uses_repository_default_branch():
    from foragent.ci_mode import _trusted_sha
    from foragent.tracker import GhTracker

    github, calls = recorded_github([])
    refs = []
    git = SimpleNamespace(ref_sha=lambda ref: refs.append(ref) or 'trusted')
    assert _trusted_sha(git, GhTracker('owner/repo', github)) == 'trusted'
    assert refs == ['origin/trunk']
    assert calls == [['api', 'repos/owner/repo']]


@pytest.mark.parametrize('fallback', [None, 'local'])
def test_missing_trusted_default_branch_obeys_fallback_configuration(tmp_path, monkeypatch, fallback):
    from conftest import BASE_TIME
    from foragent import ci_mode, ci_local
    from foragent.run_attempt import ExecutorStopped
    from foragent.tracker import TrackerError

    def unavailable():
        raise TrackerError('HTTP 403')

    local = []
    ctx = SimpleNamespace(repo='owner/repo', repo_root=tmp_path, now=lambda: BASE_TIME,
                          config=Config(checks='ci', checks_fallback=fallback, test_command='pytest'),
                          tracker=SimpleNamespace(pr_default_branch=unavailable))
    attempt = SimpleNamespace(n=1, ctx=ctx, log=tmp_path / 'log', git=SimpleNamespace())
    monkeypatch.setattr(ci_mode, '_baseline_exclusions', lambda *args: [])
    monkeypatch.setattr(ci_mode, 'flag', lambda *args: None)
    monkeypatch.setattr(ci_local, 'fallback_comparison', lambda attempt, base: local.append(base))
    if fallback:
        assert ci_mode.require_comparison(attempt, 2, 'head', base_sha='base').reports is None
        assert local == ['base']
    else:
        with pytest.raises(ExecutorStopped, match='Default branch reference unavailable') as exc:
            ci_mode.require_comparison(attempt, 2, 'head', base_sha='base')
        assert exc.value.outcome == 'held' and not local


def test_github_binary_download_preserves_bytes_and_text_errors(monkeypatch):
    import subprocess
    from foragent.tracker import GhTracker, TrackerError

    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        if args[-1].endswith('/zip'):
            return SimpleNamespace(stdout=b'PK\x03\x04\xff\x00')
        if args[-1] == 'repos/owner/repo':
            return SimpleNamespace(stdout=b'{"full_name":"owner/repo","default_branch":"main"}')
        raise subprocess.CalledProcessError(1, args, output=b'body', stderr=b'HTTP 403')

    monkeypatch.setattr(subprocess, 'run', run)
    tracker = GhTracker('owner/repo')
    assert tracker.pr_default_branch() == 'main'
    assert tracker._call(['api', 'repos/owner/repo/actions/artifacts/100/zip'], binary=True) == b'PK\x03\x04\xff\x00'
    with pytest.raises(TrackerError, match='HTTP 403') as exc:
        tracker._call(['api', 'missing'])
    assert exc.value.stdout == 'body'
    assert all(not kwargs.get('text') for _, kwargs in calls)
