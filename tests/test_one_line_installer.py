"""Check every machine mutation on installer reruns, including fresh shell PATHs."""

import os
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
SHELL = shutil.which("dash") or "/bin/sh"
LOG = '''printf '%s' "$(basename "$0")" >> "$FA_TEST_LOG"
for arg in "$@"; do printf ' %s' "$arg" >> "$FA_TEST_LOG"; done
printf '\\n' >> "$FA_TEST_LOG"
'''
UV = LOG + '''case "$*" in
  'tool dir --bin') printf '%s\\n' "$FA_TEST_BIN_DIR" ;;
  'tool install '*)
    mkdir -p "$FA_TEST_BIN_DIR"
    printf '#!/bin/sh\\necho fa installed\\n' > "$FA_TEST_BIN_DIR/fa"
    chmod +x "$FA_TEST_BIN_DIR/fa"
    printf '%s\\n' "$*" > "$HOME/uv-receipt"
    ;;
  'tool update-shell') printf 'export PATH=uv-bin:$PATH\\n' >> "$HOME/.profile" ;;
  *) exit 99 ;;
esac
'''
FETCH = LOG + '''cat <<'INSTALL_UV'
mkdir -p "$(dirname "$FA_TEST_UV_LOCATION")"
cp "$FA_TEST_UV_TEMPLATE" "$FA_TEST_UV_LOCATION"
chmod +x "$FA_TEST_UV_LOCATION"
INSTALL_UV
'''
CLAUDE = LOG + '''case "$*" in
  'plugin marketplace list --json')
    if [ -f "$HOME/marketplace" ]; then cat "$HOME/marketplace"; else echo '[]'; fi ;;
  'plugin list --json')
    if [ -f "$HOME/plugins" ]; then cat "$HOME/plugins"; else echo '[]'; fi ;;
  'plugin marketplace add JinYoon-builds/foragent') echo '[{"name": "foragent"}]' > "$HOME/marketplace" ;;
  'plugin install foragent@foragent') echo '[{"id": "foragent@foragent"}]' > "$HOME/plugins" ;;
  *) exit 99 ;;
esac
'''


def executable(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('#!/bin/sh\nset -eu\n' + body)
    path.chmod(0o755)


@pytest.fixture
def machine(tmp_path):
    home, tools = tmp_path / 'home', tmp_path / 'tools'
    home.mkdir()
    tools.mkdir()
    env = {
        'HOME': str(home), 'PATH': f'{tools}:/usr/bin:/bin',
        'FA_TEST_LOG': str(tmp_path / 'calls'),
        'FA_TEST_BIN_DIR': str(home / '.local/bin'),
        'FA_TEST_UV_LOCATION': str(home / '.local/bin/uv'),
        'FA_TEST_UV_TEMPLATE': str(tmp_path / 'uv-template'),
    }
    executable(tmp_path / 'uv-template', UV)
    executable(tools / 'curl', FETCH)
    executable(tools / 'wget', FETCH)
    executable(tools / 'claude', CLAUDE)
    return home, tools, env


def run_installer(env, launcher='sh', *, yes=True):
    if launcher == 'npm':
        node = shutil.which('node')
        if node is None:
            pytest.skip('node is not installed')
        command = [node, str(ROOT / 'npm/bin/foragent.js')]
    else:
        command = [SHELL, str(ROOT / 'install.sh')]
    result = subprocess.run(command + (['--yes'] if yes else []), env=env,
                            stdin=subprocess.DEVNULL, capture_output=True, text=True,
                            start_new_session=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout


def snapshot(home):
    return {str(path.relative_to(home)): (path.lstat().st_mtime_ns,
            os.readlink(path) if path.is_symlink() else path.read_bytes())
            for path in home.rglob('*') if path.is_file()}


@pytest.mark.parametrize('launcher', ['sh', 'npm'])
@pytest.mark.parametrize('layout', ['path', 'hidden-default', 'hidden-xdg', 'hidden-cargo', 'custom-uv-bin'])
def test_second_run_performs_no_mutation_even_in_a_fresh_shell(machine, launcher, layout):
    home, tools, env = machine
    if layout == 'path':
        executable(tools / 'uv', UV)
    elif layout == 'hidden-xdg':
        env['XDG_BIN_HOME'] = str(home / 'xdg-bin')
        env['FA_TEST_UV_LOCATION'] = str(home / 'xdg-bin/uv')
        env['FA_TEST_BIN_DIR'] = str(home / 'xdg-bin')
    elif layout == 'hidden-cargo':
        env['FA_TEST_UV_LOCATION'] = str(home / '.cargo/bin/uv')
    elif layout == 'custom-uv-bin':
        executable(tools / 'uv', UV)
        env['FA_TEST_BIN_DIR'] = str(home / 'custom tools/bin')

    run_installer(env, launcher)
    calls = Path(env['FA_TEST_LOG']).read_text().splitlines()
    assert len([c for c in calls if c.startswith('uv tool install ')]) == 1
    assert 'claude plugin marketplace add JinYoon-builds/foragent' in calls
    assert 'claude plugin install foragent@foragent' in calls
    before = snapshot(home)

    # The same original PATH models a new terminal or the next CI step.
    output = run_installer(env, launcher)
    new_calls = Path(env['FA_TEST_LOG']).read_text().splitlines()[len(calls):]
    assert new_calls == (['uv tool dir --bin'] if layout == 'custom-uv-bin' else [])
    assert snapshot(home) == before
    assert 'fa update' in output and '/fa' in output


def test_existing_versioned_fa_needs_no_uv_consent_or_plugin_changes(machine):
    home, tools, env = machine
    version = home / 'tool-versions/123/bin/fa'
    executable(version, 'echo fa versioned\n')
    target = home / '.local/bin/fa'
    target.parent.mkdir(parents=True)
    target.symlink_to(version)
    executable(tools / 'codex', LOG + 'exit 0\n')
    before = snapshot(home)
    output = run_installer(env, yes=False)
    assert not Path(env['FA_TEST_LOG']).exists(), 'No tool needs to run for an installed fa'
    assert snapshot(home) == before
    assert target.is_symlink() and target.resolve() == version
    assert 'fa update' in output and '/fa' in output
    assert 'Codex as PM is experimental' in output


@pytest.mark.parametrize('existing', [False, True])
def test_first_cli_install_only_registers_the_exact_missing_plugin(machine, existing):
    home, tools, env = machine
    executable(tools / 'uv', UV)
    (home / 'marketplace').write_text('[{"name": "foragent"}]' if existing
                                    else '[{"name": "foragent-fork"}]')
    (home / 'plugins').write_text('[{"id": "foragent@foragent"}]' if existing
                                else '[{"id": "foragent@foragent-fork"}]')
    before = snapshot(home)
    run_installer(env)
    calls = Path(env['FA_TEST_LOG']).read_text().splitlines()
    assert ('claude plugin marketplace add JinYoon-builds/foragent' in calls) is not existing
    assert ('claude plugin install foragent@foragent' in calls) is not existing
    if existing:
        after = snapshot(home)
        assert after['marketplace'] == before['marketplace']
        assert after['plugins'] == before['plugins']
