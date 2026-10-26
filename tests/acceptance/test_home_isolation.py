"""Acceptance test for home-isolation. Locked by Foragent; do not edit."""

import os
from pathlib import Path
import pwd
import subprocess
import sys

REPO = Path(__file__).resolve().parents[2]
ACCOUNT_HOME = Path(pwd.getpwuid(os.getuid()).pw_dir)
REAL_INIT_TESTS = ["tests/acceptance/test_board_projects.py", "tests/acceptance/test_view_open_triggers.py"]


def test_every_test_runs_with_a_temporary_home_without_asking_for_it():
    # This test requests no fixture: isolation must be automatic for every test.
    home = Path.home().resolve()
    assert home != ACCOUNT_HOME.resolve(), "Tests must never run with the account's real HOME"
    for key in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME", "GIT_CONFIG_GLOBAL"):
        value = os.environ.get(key)
        assert value, f"{key} must be set to a temporary location"
        assert Path(value).resolve().is_relative_to(home), (key, value, home)


def test_real_fa_init_tests_never_touch_the_starting_home(tmp_path):
    """Run the tests that call a real fa init under a pretend person's HOME and
    prove that nothing in that HOME changes."""
    person = tmp_path / "person"
    config = person / ".config/foragent/config.yml"
    config.parent.mkdir(parents=True)
    config.write_text("skill_libraries: []\nlanguage: ko\n", encoding="utf-8")
    before = {path: path.read_bytes() for path in person.rglob("*") if path.is_file()}

    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith(("XDG_", "GIT_CONFIG", "PYTEST_"))}
    environment.update(HOME=str(person))
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *REAL_INIT_TESTS],
        cwd=REPO, env=environment, capture_output=True, text=True, timeout=900,
    )
    assert result.returncode == 0, result.stdout[-4000:] + result.stderr[-2000:]
    after = {path: path.read_bytes() for path in person.rglob("*") if path.is_file()}
    assert after == before, sorted(str(path) for path in set(after) ^ set(before)) or "a file changed"
