"""People can find the first action from public CLI output."""

import os
from pathlib import Path
import subprocess
import sys


SOURCE = Path(__file__).resolve().parents[2] / 'src'


def invoke(tmp_path, *args):
    result = subprocess.run([sys.executable, '-m', 'foragent.cli', *args], cwd=tmp_path,
                            env={**os.environ, 'PYTHONPATH': str(SOURCE)},
                            capture_output=True, text=True)
    return result.returncode, result.stdout + result.stderr


def test_help_separates_person_commands_and_shows_where_to_start(tmp_path):
    code, output = invoke(tmp_path, '--help')
    assert code == 0
    assert 'start with fa init, then /fa' in output.lower(), output
    # These headings describe the two audiences; the command names stay callable.
    person = output.lower().index('person commands:')
    internal = output.lower().index('internal commands:')
    assert person < internal
    visible = {line.split()[0] for line in output[person:internal].splitlines() if line.split()}
    workers = {line.split()[0] for line in output[internal:].splitlines() if line.split()}
    assert {'init', 'doctor', 'inspect', 'gate'} <= visible
    assert {'role', 'plan', 'orch', 'ci', 'tidy', 'test', 'bench'} <= workers
    assert not {'role', 'plan', 'orch', 'ci', 'tidy', 'test', 'bench'} & visible


def test_doctor_before_init_names_the_next_command(tmp_path):
    code, output = invoke(tmp_path, 'doctor')
    assert code != 0
    assert '.foragent.yml' in output and 'fa init' in output, output
    assert 'Traceback' not in output
    assert not list(tmp_path.iterdir()), 'doctor must leave the new project alone'
