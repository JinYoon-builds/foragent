"""Preview lifecycle failures must preserve the user's existing work."""

import json
import subprocess

import pytest

from foragent import cli, design_session
from foragent.config import Config
from foragent.state import StateStore


def local_cli(tmp_path, monkeypatch):
    root = tmp_path / 'repo'
    root.mkdir()
    store = StateStore(tmp_path / 'state')
    monkeypatch.setattr(cli, 'load_config', lambda root: Config(repo='owner/repo'))
    monkeypatch.setattr(StateStore, 'for_repo', classmethod(lambda cls, repo: store))
    return root, store


@pytest.mark.parametrize('args', [
    ['--stage', 'board'],
    ['--command', ''],
    ['--url', 'javascript:alert(1)'],
    ['--url', 'file:///tmp/index.html'],
    ['--url', 'http://[invalid'],
    ['--url', 'http://localhost:invalid'],
    ['--command', 'a', '--command', 'b', '--command', 'c', '--command', 'd'],
])
def test_invalid_preview_keeps_current_panes(tmp_path, monkeypatch, capsys, args):
    root, store = local_cli(tmp_path, monkeypatch)
    current = {'panes': [{'kind': 'orca', 'executable': '/offline/orca', 'handle': 'current'}]}
    path = store.root / 'design/session.json'
    store._write_json(path, current)
    monkeypatch.setattr(subprocess, 'run', lambda *a, **kw: pytest.fail('invalid input must not close panes'))
    assert cli.main(['design', 'preview', '--repo-root', str(root), *args]) == 1
    assert json.loads(path.read_text()) == current
    assert 'foragent:' in capsys.readouterr().err


def test_picks_topics_are_isolated_and_cannot_escape_state(tmp_path):
    store = StateStore(tmp_path / 'state')
    path = design_session._topic(store, '../../elsewhere') / 'picks.json'
    store._write_json(path, {'likes': ['airy'], 'dislikes': []})
    assert path.resolve().is_relative_to(store.root.resolve())
    assert design_session.picks(StateStore(store.root), '../../elsewhere')['likes'] == ['airy']
    assert design_session.picks(store, 'other') == {'likes': [], 'dislikes': []}


def test_partial_terminal_open_is_recorded_for_later_cleanup(tmp_path, monkeypatch):
    root, store = local_cli(tmp_path, monkeypatch)
    monkeypatch.setattr(design_session.watch_open, '_recorded_terminal', lambda *a: ('orca', 'lane-view'))
    calls = []

    def call(argv, root):
        calls.append(argv)
        if 'split' in argv:
            if len(calls) > 1:
                raise subprocess.CalledProcessError(1, argv)
            return subprocess.CompletedProcess(argv, 0, stdout=json.dumps({'handle': 'draft-a'}))
        return subprocess.CompletedProcess(argv, 0, stdout='')

    monkeypatch.setattr(design_session, '_terminal_call', call)
    assert cli.main(['design', 'preview', '--repo-root', str(root),
                     '--command', 'python a.py', '--command', 'python b.py']) == 1
    record = json.loads((store.root / 'design/session.json').read_text())
    assert [pane['handle'] for pane in record['panes']] == ['draft-a']
    assert cli.main(['design', 'done', '--repo-root', str(root)]) == 0
    assert calls[-1] == ['orca', 'terminal', 'close', '--terminal', 'draft-a', '--json']
    before = len(calls)
    assert cli.main(['design', 'done', '--repo-root', str(root)]) == 0
    assert len(calls) == before


def test_failed_close_retains_handle_for_retry(tmp_path, monkeypatch):
    root, store = local_cli(tmp_path, monkeypatch)
    record = {'panes': [{'kind': 'orca', 'executable': 'orca', 'handle': 'draft-a'}]}
    path = store.root / 'design/session.json'
    store._write_json(path, record)

    def fail(argv, root):
        raise subprocess.TimeoutExpired(argv, 10)

    monkeypatch.setattr(design_session, '_terminal_call', fail)
    assert cli.main(['design', 'done', '--repo-root', str(root)]) == 1
    assert json.loads(path.read_text()) == record
