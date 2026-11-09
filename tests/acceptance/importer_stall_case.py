"""Shared fixture for the importer check's stall window (r1305, 11-09).

A small project under publication has a shared acceptance file the order changes and one test file
that imports it. The importer's tests sleep in short steps and append one line to a progress file
outside the copy each time a test finishes, so a test can see that every run reported its tests.

Two clocks are used, never minutes of waiting:
- The check's clock (``time.monotonic`` as the ``foragent.order_collect*`` modules see it) advances
  STEP_S seconds for every test the importer runs have finished. Collection finishes no test, so
  the 30 s collection budget is untouched.
- The stall window is set small by the test through ``order_collect.IMPORTERS_STALL_S`` (seconds).

Only Git transport, GitHub and the preread model are faked (see test_publish_collect_check.py).
The real collection and importer check run; the repository's conftest switches the check off
outside its own files, so this fixture switches the real one back on for these tests.
"""

import re
import sys
import time
from pathlib import Path

import pytest

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "src/foragent").is_dir())
for directory in (ROOT / "tests", ROOT / "tests/acceptance"):
    if str(directory) not in sys.path:
        sys.path.append(str(directory))

from foragent import order_collect  # noqa: E402
from test_publish_collect_check import call, collection_case, snapshot  # noqa: E402, F401

# Captured at import, before the repository conftest replaces it for files outside its list.
REAL_CHECK = order_collect.check

SHARED = "tests/acceptance/test_importer_pace_shared.py"
IMPORTER = "tests/test_importer_pace_user.py"
BROKEN_TEST = "test_shared_value_still_holds"
QUIET_TEST = "test_zz_goes_quiet_in_one_run"
# Seconds of the check's clock per finished importer test.
STEP_S = 25.0
SILENCE_WORDS = re.compile(
    r"no (test )?progress|no finished test|reported nothing|silent|silence|stall|stopped reporting", re.I)


def shared_text(*, where, silent_in="none", broken=False):
    return (
        f"SILENT_IN = {silent_in!r}\n"
        f"WHERE = {where!r}\n"
        f"BROKEN = {broken!r}\n\n\n"
        "def test_pace_shared():\n"
        "    assert WHERE in ('base', 'order')\n"
    )


def importer_text(progress, *, steps, pause, silent):
    return (
        "import pathlib, sys, time\n\n"
        "import pytest\n\n"
        "ROOT = pathlib.Path(__file__).resolve().parents[1]\n"
        "if str(ROOT / 'tests' / 'acceptance') not in sys.path:\n"
        "    sys.path.insert(0, str(ROOT / 'tests' / 'acceptance'))\n\n"
        "from test_importer_pace_shared import BROKEN, SILENT_IN, WHERE\n\n"
        f"PROGRESS = pathlib.Path({str(progress)!r})\n\n\n"
        "def finished(name):\n"
        "    with PROGRESS.open('a', encoding='utf-8') as out:\n"
        "        out.write(f'{WHERE} {name}\\n')\n\n\n"
        f"@pytest.mark.parametrize('step', range({steps}))\n"
        "def test_keeps_reporting(step):\n"
        "    for _ in range(3):\n"
        f"        time.sleep({pause})\n"
        "    finished(f'step {step}')\n\n\n"
        f"def {BROKEN_TEST}():\n"
        "    assert not BROKEN, 'the order broke what this importer needs'\n"
        "    finished('value')\n\n\n"
        f"def {QUIET_TEST}():\n"
        "    if SILENT_IN == WHERE:\n"
        f"        time.sleep({silent})\n"
        "    finished('quiet')\n"
    )


class _Clock:
    """The time module with a monotonic clock that jumps STEP_S per finished importer test."""

    def __init__(self, progress):
        self._progress = progress
        self._real = time.monotonic

    def finished(self):
        try:
            return len(self._progress.read_text(encoding="utf-8").splitlines())
        except FileNotFoundError:
            return 0

    def monotonic(self):
        return self._real() + STEP_S * self.finished()

    def __getattr__(self, name):
        return getattr(time, name)


@pytest.fixture
def stall_case(collection_case, monkeypatch, tmp_path):
    case = collection_case
    monkeypatch.setattr(order_collect, "check", REAL_CHECK)
    case.progress = tmp_path / "importer-progress.txt"

    def build(*, order_where="order", silent_in="none", broken=False, steps=6, pause=0.03, silent=0):
        case.history.update({
            SHARED: shared_text(where="base", silent_in=silent_in),
            IMPORTER: importer_text(case.progress, steps=steps, pause=pause, silent=silent),
        })
        case.git.update_base("main")
        item = case.plan["issues"][0]
        item["files"] = ["candidate.py", SHARED]
        item["done_when"] = [f"uv run pytest -q {SHARED}"]
        item["acceptance_files"] = {SHARED: shared_text(where=order_where, silent_in=silent_in, broken=broken)}
        return case

    def use_check_clock():
        """Advance the check's clock STEP_S seconds per finished importer test."""
        clock = _Clock(case.progress)
        patched = 0
        for name, module in list(sys.modules.items()):
            if name.startswith("foragent.order_collect") and getattr(module, "time", None) is time:
                monkeypatch.setattr(module, "time", clock)
                patched += 1
        assert patched, "No foragent.order_collect* module reads the time module"
        return clock

    def lines(where):
        try:
            text = case.progress.read_text(encoding="utf-8")
        except FileNotFoundError:
            return []
        return [line for line in text.splitlines() if line.startswith(f"{where} ")]

    case.build, case.use_check_clock, case.lines = build, use_check_clock, lines
    return case


def set_stall_window(monkeypatch, seconds):
    # raising=False: on main the setting does not exist yet, and the test fails by its assertions.
    monkeypatch.setattr(order_collect, "IMPORTERS_STALL_S", seconds, raising=False)


def timed_call(case, capsys, action, *options):
    started = time.monotonic()
    code, diagnostic = call(case, capsys, action, *options)
    return code, diagnostic, time.monotonic() - started
