"""CI evidence validation, workflow generation and portable shard measurement."""

from copy import deepcopy
from contextlib import contextmanager
from dataclasses import replace
from datetime import timedelta
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest
import yaml

from foragent.ci_mode import UnusableComparison, comparison
from foragent.ci_workflow import PUBLISH_WORKFLOW, WORKFLOW, measurement_scope, workflow_text, write_workflow
from foragent.config import Config, ConfigError, load_config
from runner_support import run_case


def report(index=0, count=2):
    node = f"tests/acceptance/test_keep_{index}.py::test_keep"
    payload = {"schema_version": 1, "repository": "owner/repo", "base_sha": "base", "head_sha": "head",
               "shard_index": index, "shard_count": count, "excluded": [], "duration_seconds": index + 2,
               "scope": measurement_scope(Config(ci_shards=count), Path('.'), []),
               "base": {"complete": True, "collected_ids": [node], "passing_ids": [node]},
               "head": {"complete": True, "collected_ids": [node], "passing_ids": [node]}}
    return {"name": f"foragent-acceptance/{index}", "head_sha": "head", "status": "completed", "conclusion": "success",
            "app": {"slug": "github-actions"}, "details_url": "https://github.com/owner/repo/actions/runs/42",
            "check_suite": {"id": 420},
            "workflow_run": {"id": 42, "check_suite_id": 420, "head_branch": "main",
                             "repository": {"full_name": "owner/repo", "default_branch": "main"},
                             "path": PUBLISH_WORKFLOW, "event": "workflow_run"},
            "output": {"text": json.dumps(payload)}}


def compare(checks, count=2):
    return comparison(checks, "owner/repo", "base", "head", [],
                      expected_scope=measurement_scope(Config(ci_shards=count), Path('.'), []),
                      expected_shards=range(count))


def test_compare_records_parallel_wall_duration_and_detects_deselection():
    checks = [report(), report(1)]
    payload = json.loads(checks[1]["output"]["text"])
    payload["head"] = {"complete": True, "collected_ids": [], "passing_ids": []}
    checks[1]["output"]["text"] = json.dumps(payload)
    lost, duration = compare(checks)
    assert lost == ["tests/acceptance/test_keep_1.py::test_keep"]
    assert duration == 3


@pytest.mark.parametrize("damage", ["check-sha", "nan", "overlap", "duplicate", "malformed", "cancelled"])
def test_rejects_unusable_shards(damage):
    checks = [report(), report(1)]
    payload = json.loads(checks[1]["output"]["text"])
    if damage == "check-sha":
        checks[1]["head_sha"] = "old"
    elif damage == "nan":
        payload["duration_seconds"] = float('nan')
    elif damage == "overlap":
        payload["base"] = json.loads(checks[0]["output"]["text"])["base"]
    elif damage == "duplicate":
        checks[1] = deepcopy(checks[0])
        payload = json.loads(checks[1]["output"]["text"])
    elif damage == "cancelled":
        checks[1]["conclusion"] = "cancelled"
    checks[1]["output"]["text"] = "{" if damage == "malformed" else json.dumps(payload)
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
    assert document['permissions']['checks'] == 'write'
    path.write_text('obsolete workflow')
    write_workflow(tmp_path, config, warn=False)
    assert path.read_text() == workflow_text(config)
    assert len(calls) == 1


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
    from foragent.tracker import GhTracker

    calls = []

    def github(args, input=None):
        calls.append(args)
        return json.dumps({'head': {'sha': 'head', 'repo': {'full_name': 'fork/repo'}}})

    assert GhTracker('owner/repo', github).pr_acceptance_checks(1) == []
    assert len(calls) == 1


def test_local_only_base_failures_are_ignored_but_head_unit_failures_are_not(git_history, tmp_path, monkeypatch):
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
    config = Config(checks='ci', test_command=f'{sys.executable} -m pytest')
    ctx = RunContext('owner/repo', root, FakeTracker(), store, config, GitRepo(root), lambda: datetime.now(UTC))
    # Exercise a detached base via the existing no-commit history fixture.
    git_history.update({'tests/acceptance/test_machine.py': acceptance.read_text()})
    git_command(root, 'fetch', '-q', 'origin')
    baseline = capture_local_only(1, ctx, ctx.git, config.test_command, git_history.tip, [])
    assert baseline.node_ids == ('tests/acceptance/test_machine.py::test_good',)
    attempt = SimpleNamespace(n=1, ctx=ctx, worktree=root)
    result = verify_local_only_units(attempt, baseline.command)
    assert result.exit_code == 1


def test_tracker_reads_every_page_of_head_check_runs():
    from foragent.tracker import GhTracker

    calls = []

    def github(args, input=None):
        calls.append(args)
        if args[-1].endswith('/pulls/2'):
            return json.dumps({'head': {'sha': 'head', 'repo': {'full_name': 'owner/repo'}}})
        if args[-1].endswith('/actions/runs/42'):
            return json.dumps(report()['workflow_run'])
        if 'check_suite_id=420' in args[-1]:
            return json.dumps([report()['workflow_run']])
        assert '--paginate' in args and args[args.index('--jq') + 1] == '.check_runs'
        assert '/commits/head/check-runs?' in args[-1]
        return json.dumps([report()]) + '\n' + json.dumps([report(1)])

    assert len(GhTracker('owner/repo', github).pr_acceptance_checks(2)) == 2
    assert len(calls) == 4


def test_local_mode_generated_workflow_does_not_require_ci_inputs(monkeypatch, tmp_path, capsys):
    from foragent.ci_shard import main

    monkeypatch.chdir(tmp_path)
    (tmp_path / '.foragent.yml').write_text('checks: local\n')
    main()
    assert 'locally' in capsys.readouterr().out


def test_large_comparisons_fit_github_output_without_losing_any_ids():
    from foragent.ci_transport import LIMIT, encode_report

    check = report(count=1)
    payload = json.loads(check['output']['text'])
    ids = [f'tests/acceptance/test_large.py::TestRegression::test_case[parameter-{index}]' for index in range(1500)]
    for side in ('base', 'head'):
        payload[side] = {'complete': True, 'collected_ids': ids, 'passing_ids': ids}
    payload['head']['passing_ids'] = ids[:-1]
    check['output']['text'] = encode_report(payload)
    assert len(check['output']['text'].encode()) <= LIMIT
    assert json.loads(check['output']['text'])['encoding'] == 'zlib+base64'
    lost, _ = compare([check], count=1)
    assert lost == [ids[-1]]


def test_damaged_compressed_report_is_unusable():
    check = report(count=1)
    check['output']['text'] = json.dumps({'encoding': 'zlib+base64', 'excluded': [], 'data': 'eA=='})
    with pytest.raises(UnusableComparison):
        compare([check], count=1)


def test_shard_publishes_real_base_and_head_measurements(git_history, tmp_path, monkeypatch):
    from conftest import git_command
    from foragent import ci_publish, ci_shard
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
    posted = []

    class Response:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def read(self):
            return b'{}'

    def publish(request):
        assert request.full_url == 'https://api.github.com/repos/owner/repo/check-runs'
        assert request.get_method() == 'POST'
        posted.append(json.loads(request.data))
        return Response()

    monkeypatch.setattr(ci_publish, 'urlopen', publish)
    ci_shard.main()
    assert posted == []
    ci_publish.publish(decode_report((tmp_path / 'shard-0.json').read_text()), 'owner/repo', head,
                       'https://github.com/owner/repo/actions/runs/42')
    assert len(posted) == 1 and posted[0]['head_sha'] == head
    payload = decode_report(posted[0]['output']['text'])
    assert payload['base_sha'] == base and payload['head_sha'] == head
    assert payload['base']['complete'] and payload['head']['complete']
    assert f'{keep}::test_keep' in payload['base']['passing_ids']
    assert f'{keep}::test_keep' not in payload['head']['passing_ids']
    assert decode_report(output.read_text().removeprefix('comparison=')) == payload


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
    check.update(head_sha=head, output={'text': artifact.read_text()})
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
    monkeypatch.setattr(case.tracker, 'pr_acceptance_checks', lambda pr: [])

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


@pytest.mark.parametrize('damage', ['unrelated-app', 'foreign-suite', 'missing-app', 'wrong-workflow',
                                         'wrong-repository', 'wrong-event', 'wrong-run', 'wrong-sha', 'wrong-name'])
def test_unrelated_producers_cannot_supply_acceptance_evidence(damage):
    checks = [report(), report(1)]
    for check in checks:
        if damage == 'unrelated-app':
            check['app'] = {'slug': 'unrelated-publisher'}
        elif damage == 'foreign-suite':
            check['check_suite']['id'] = 999
        elif damage == 'missing-app':
            check.pop('app')
        elif damage == 'wrong-workflow':
            check['workflow_run']['path'] = '.github/workflows/unrelated.yml'
        elif damage == 'wrong-repository':
            check['workflow_run']['repository']['full_name'] = 'owner/other'
        elif damage == 'wrong-event':
            check['workflow_run']['event'] = 'pull_request'
        elif damage == 'wrong-run':
            check['workflow_run']['id'] = 43
            check['workflow_run']['check_suite_id'] = 430
        elif damage == 'wrong-sha':
            check['head_sha'] = 'old'
        else:
            check['name'] += '-edited'
    with pytest.raises(UnusableComparison):
        compare(checks)


def test_measurement_cannot_read_publication_credentials(tmp_path, monkeypatch):
    from foragent.ci_shard import measure
    from foragent.ci_workflow import publisher_workflow_text

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
    publisher = yaml.safe_load(publisher_workflow_text(Config()))
    assert 'workflow_run' in publisher['on']
    assert publisher['permissions']['checks'] == 'write'
    assert all(step.get('uses') != 'actions/checkout@v4' for step in publisher['jobs']['publish']['steps'])


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
    check.update(head_sha=head, output={'text': artifact.read_text()})
    assert comparison([check], 'owner/repo', base, head, [], expected_scope=scope, expected_shards=[0])[0] == [keep]
    payload['scope']['acceptance_dir'] = 'nonexistent'
    for side in ('base', 'head'):
        payload[side] = {'complete': True, 'collected_ids': [], 'passing_ids': []}
    check['output']['text'] = json.dumps(payload)
    with pytest.raises(UnusableComparison):
        comparison([check], 'owner/repo', base, head, [], expected_scope=scope, expected_shards=[0])


def test_report_cannot_shrink_the_expected_four_shard_layout():
    check = report(count=1)
    payload = json.loads(check['output']['text'])
    payload['scope'] = measurement_scope(Config(ci_shards=4), Path('.'), [])
    check['output']['text'] = json.dumps(payload)
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
    from foragent import ci_publish

    run = {'path': WORKFLOW, 'event': 'pull_request', 'repository': {'full_name': 'owner/repo'},
           'head_repository': {'full_name': 'owner/repo'}, 'head_sha': 'head', 'conclusion': 'success'}
    if damage == 'foreign-repository':
        run['head_repository']['full_name'] = 'fork/repo'
    elif damage == 'wrong-workflow':
        run['path'] = '.github/workflows/other.yml'
    elif damage == 'wrong-event':
        run['event'] = 'push'
    elif damage == 'foreign-head':
        run['head_sha'] = 'other-head'
    event = tmp_path / 'event.json'
    event.write_text(json.dumps({'workflow_run': run}))
    for index in range(2):
        payload = json.loads(report(index)['output']['text'])
        if damage == 'no-shards' or (damage == 'missing-shard' and index == 1):
            continue
        if damage == 'wrong-shard' and index == 1:
            payload['shard_index'] = 0
        (tmp_path / f'shard-{index}.json').write_text(json.dumps(payload))
    for name, value in {'FA_REPOSITORY': 'owner/repo', 'FA_SHARDS': '2', 'FA_REPORT_DIR': str(tmp_path),
                        'GITHUB_EVENT_PATH': str(event), 'GITHUB_RUN_ID': '42'}.items():
        monkeypatch.setenv(name, value)
    posted = []
    monkeypatch.setattr(ci_publish, 'publish', lambda *args: posted.append(args))
    if damage == 'no-shards':
        ci_publish.main()
        assert not posted
    elif damage:
        with pytest.raises(ValueError):
            ci_publish.main()
        assert not posted
    else:
        ci_publish.main()
        assert [args[0]['shard_index'] for args in posted] == [0, 1]
        assert all(args[1:] == ('owner/repo', 'head', 'https://github.com/owner/repo/actions/runs/42') for args in posted)


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


@pytest.mark.parametrize('workflow', [WORKFLOW, PUBLISH_WORKFLOW, '.github/workflows/foragent-acceptance-extra.yml'])
@pytest.mark.parametrize('regressed', [False, True])
def test_changed_acceptance_workflow_requires_local_comparison_even_with_fabricated_shards(
    run_case, monkeypatch, workflow, regressed,
):
    from foragent.runner import run_issue

    case = run_case
    case.ctx.config = replace(case.ctx.config, checks='ci', checks_fallback=None, max_retries=0, ci_shards=2,
                              test_command=f'{sys.executable} -m pytest')
    case.history.update({WORKFLOW: 'name: measurement\non: [pull_request]\n',
                         PUBLISH_WORKFLOW: 'name: publish\non: [workflow_run]\n',
                         'src/__init__.py': '', 'src/demo.py': 'VALUE = 1\n',
                         'tests/acceptance/test_keep.py':
                             'from src.demo import VALUE\ndef test_keep(): assert VALUE == 1\n'})
    case.executor.steps = [{'feature.txt': 'done', workflow: 'name: fabricated uploads\non: [pull_request]\n',
                            'src/demo.py': f'VALUE = {2 if regressed else 1}\n'}]
    forged = [report(), report(1)]
    for check in forged:
        payload = json.loads(check['output']['text'])
        for side in ('base', 'head'):
            payload[side] = {'complete': True, 'collected_ids': [], 'passing_ids': []}
        check['output']['text'] = json.dumps(payload)
    reads = []

    def forged_checks(pr):
        from foragent.ci_workflow import EXCLUSIONS

        reads.append(pr)
        head = case.tracker.pr_head_sha(pr)
        excluded = json.loads((case.store.worktree_path(1) / EXCLUSIONS).read_text())
        for check in forged:
            check['head_sha'] = head
            payload = json.loads(check['output']['text'])
            payload.update(base_sha=case.history.tip, head_sha=head, excluded=excluded,
                           scope=measurement_scope(case.ctx.config, case.ctx.repo_root, excluded))
            check['output']['text'] = json.dumps(payload)
        return forged

    monkeypatch.setattr(case.tracker, 'pr_acceptance_checks', forged_checks)

    outcome = run_issue(1, case.ctx)
    assert (outcome == 'merged') is not regressed
    assert not reads, 'A changed workflow must never supply CI evidence for this head'
    assert len(case.executor.calls) == 1
    if not regressed:
        assert case.store.get_run(1).comparison['source'] == 'local'
    else:
        assert 'test_keep' in case.store.log_path(1).read_text()


@pytest.mark.parametrize('damage', ['unrelated-workflow', 'different-suite', 'non-default-branch', None])
def test_tracker_binds_borrowed_publisher_url_to_github_suite_and_run_ids(damage):
    from foragent.tracker import GhTracker

    check = report(count=1)
    check['check_suite'] = {'id': 990}
    run = {**check['workflow_run'], 'id': 99, 'check_suite_id': 990}
    if damage == 'unrelated-workflow':
        run['path'] = '.github/workflows/unrelated.yml'
    elif damage == 'different-suite':
        run['check_suite_id'] = 420
    elif damage == 'non-default-branch':
        run['head_branch'] = 'foragent/issue-1'
    calls = []

    def github(args, input=None):
        endpoint = args[-1]
        calls.append(endpoint)
        if endpoint.endswith('/pulls/2'):
            return json.dumps({'head': {'sha': 'head', 'repo': {'full_name': 'owner/repo'}}})
        if '/commits/head/check-runs?' in endpoint:
            return json.dumps([check])
        if 'check_suite_id=990' in endpoint:
            return json.dumps([{'id': 99}])
        assert endpoint.endswith('/actions/runs/99'), endpoint
        return json.dumps(run)

    checks = GhTracker('owner/repo', github).pr_acceptance_checks(2)
    assert all(not endpoint.endswith('/actions/runs/42') for endpoint in calls)
    if damage:
        with pytest.raises(UnusableComparison):
            compare(checks, count=1)
    else:
        assert compare(checks, count=1)[0] == []


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

    def checks(pr):
        reads.append(holder[0])
        head = case.tracker.pr_head_sha(pr)
        heads.add(head)
        if changed[0] and change == 'missing':
            return []
        excluded = json.loads((case.store.worktree_path(1) / EXCLUSIONS).read_text())
        values = [report(), report(1)]
        for value in values:
            value['head_sha'] = head
            if changed[0] and change == 'producer':
                value['workflow_run']['id'] = 43
            payload = json.loads(value['output']['text'])
            payload.update(base_sha=case.history.tip, head_sha=head, excluded=excluded,
                           scope=measurement_scope(case.ctx.config, case.ctx.repo_root, excluded))
            if changed[0] and change == 'regression':
                payload['head']['passing_ids'] = []
            value['output']['text'] = json.dumps(payload)
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

    monkeypatch.setattr(case.tracker, 'pr_acceptance_checks', checks)
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
