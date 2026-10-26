"""Acceptance test for ci-unit-shards. Locked by Foragent; do not edit."""

from collections import Counter
from importlib import import_module
from itertools import combinations
import os
from pathlib import Path
import shlex
import subprocess
import sys

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[2]
MODULE = "foragent.ci_unit_shard"


def _select():
    # Import inside tests so a missing public API fails a case, not collection.
    return import_module(MODULE).select


def _run_cli(root, shard, shards):
    # Observe real pytest startup, including a mistaken invocation with no paths.
    # PYTEST_PLUGINS works for pytest.main, python -m pytest and its console script.
    (root / "ci_unit_shards_probe.py").write_text(
        "from pathlib import Path\n"
        "def pytest_configure(config):\n"
        f"    Path({str(root / 'pytest-started')!r}).touch()\n",
        encoding="utf-8",
    )
    environment = {
        **os.environ,
        "FA_SHARD": str(shard),
        "FA_SHARDS": str(shards),
        "PYTHONPATH": os.pathsep.join((str(root), str(ROOT / "src"))),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        "PYTEST_PLUGINS": "ci_unit_shards_probe",
        "PYTEST_ADDOPTS": "-p no:cacheprovider",
    }
    # No timeout: subprocess timeout cleanup would deliver a real process signal.
    return subprocess.run(
        [sys.executable, "-m", MODULE], cwd=root, env=environment,
        capture_output=True, text=True, check=False,
    )


@pytest.fixture
def unit_project(tmp_path):
    paths = [
        "tests/test_zulu.py", "tests/test_alpha.py", "tests/test_middle.py",
        "tests/nested/test_bravo.py", "tests/nested/test_yankee.py",
        "tests/deeper/nested/test_charlie.py",
        "tests/acceptance_helpers/test_delta.py",
    ]
    for relative in paths:
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "from pathlib import Path\nimport pytest\n"
            "def test_unit():\n"
            f"    with Path({str(tmp_path / 'executed')!r}).open('a') as stream:\n"
            f"        stream.write({relative + chr(10)!r})\n"
            f"    assert not Path({str(tmp_path / 'fail-units')!r}).exists()\n"
            "@pytest.mark.local_only\n"
            "def test_machine_only():\n"
            "    raise AssertionError('local_only must be deselected')\n",
            encoding="utf-8",
        )
    # These are deliberately uncollectable: discovery must stay within unit files.
    for relative in ("tests/acceptance/test_trap.py", "tests/helper.py", "src/test_trap.py"):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("this is not valid Python!\n", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\nmarkers = ["local_only: machine integration"]\n',
        encoding="utf-8",
    )
    return tmp_path, paths


def test_ci_unit_shards_partition_real_unit_files_once_and_independently_of_order():
    select = _select()
    paths = sorted(
        path.relative_to(ROOT).as_posix()
        for path in (ROOT / "tests").rglob("test_*.py")
        if path.is_file() and not path.is_relative_to(ROOT / "tests/acceptance")
    )
    assert len(paths) >= 6, "Exercise all six shard counts on the real unit suite"
    orders = [paths, paths[::-1], paths[::2] + paths[1::2]]
    for shards in range(1, 7):
        partitions = [select(list(paths), shard, shards) for shard in range(shards)]
        assert all(isinstance(part, list) and part for part in partitions), (
            f"Every shard must receive files when files >= shards ({shards=})"
        )
        assert Counter(path for part in partitions for path in part) == Counter(paths), (
            f"Every real unit file must occur exactly once ({shards=})"
        )
        assert all(set(left).isdisjoint(right) for left, right in combinations(partitions, 2))
        for order in orders:
            assert [select(list(order), shard, shards) for shard in range(shards)] == partitions, (
                f"Selection and result order must be deterministic, regardless of input order ({shards=})"
            )


def test_ci_unit_shards_workflow_keeps_collection_and_runs_four_failing_shards():
    document = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
    assert document["name"] == "ci", "The merge gate identifies this workflow by name"
    jobs = document["jobs"]
    steps = [(job, step, shlex.split(step["run"]))
             for job in jobs.values() for step in job.get("steps", []) if "run" in step]
    collection = [(job, step, words) for job, step, words in steps if "--collect-only" in words]
    assert len(collection) == 1, "Keep one whole-suite collection step in the workflow definition"
    collect_job, collect_step, collect_words = collection[0]
    assert collect_words[:2] == ["uv", "run"] and "pytest" in collect_words
    assert set(collect_words[collect_words.index("pytest") + 1:]) <= {
        "--collect-only", "-q", ".", "tests",
    }, "Collection must cover the whole suite, including acceptance, without filters or masked errors"
    assert collect_job.get("continue-on-error", False) is False
    assert collect_step.get("continue-on-error", False) is False

    sharded = [job for job in jobs.values() if "shard" in job.get("strategy", {}).get("matrix", {})]
    assert len(sharded) == 1, "Run the unit suite through one four-shard matrix job"
    job = sharded[0]
    matrix = job["strategy"]["matrix"]
    assert set(matrix) == {"shard"}, "Extra matrix axes or exclusions must not repeat or drop unit files"
    assert len(matrix["shard"]) == 4 and set(matrix["shard"]) == {0, 1, 2, 3}
    assert job.get("continue-on-error", False) is False, "A failed shard must fail ci"
    shard_steps = [(step, words) for owner, step, words in steps if owner is job and MODULE in words]
    assert len(shard_steps) == 1, "Each matrix shard must invoke the public unit-shard entry point"
    step, words = shard_steps[0]
    assert words[:2] == ["uv", "run"]
    module_index = words.index(MODULE)
    assert words[module_index - 2:module_index] == ["python", "-m"]
    assert not any("--ignore" in word for word in words), "Shard execution must not exclude unit files"
    assert not any(word in {"||", ";", "&"} for word in words), "Propagate the shard's failure"
    assert step.get("continue-on-error", False) is False
    environment = {**document.get("env", {}), **job.get("env", {}), **step.get("env", {})}
    assert str(environment.get("FA_SHARD", "")).replace(" ", "") == "${{matrix.shard}}"
    assert str(environment.get("FA_SHARDS")) == "4"
    assert all("pytest" not in argv for _, other, argv in steps
               if other is not collect_step and other is not step), "Do not also run the unsharded unit suite"


def test_ci_unit_shards_cli_discovers_only_its_files_filters_local_only_and_propagates_exit(unit_project):
    root, paths = unit_project
    executed = root / "executed"
    for shard in range(4):
        executed.unlink(missing_ok=True)
        result = _run_cli(root, shard, 4)
        assert result.returncode == 0, result.stdout + result.stderr
        select = _select()
        expected = select(list(paths), shard, 4)
        assert expected, "Seven unit files must populate all four shards"
        observed = executed.read_text(encoding="utf-8").splitlines() if executed.exists() else []
        assert Counter(observed) == Counter(expected), (
            f"The CLI must run exactly its selected files once ({shard=}): {observed=} {expected=}"
        )
    assert (root / "pytest-started").is_file(), "Exercise real pytest, not a fake success"
    (root / "fail-units").touch()
    executed.unlink()
    result = _run_cli(root, 0, 4)
    assert result.returncode == 1, "A pytest test failure must remain exit code 1:\n" + result.stdout + result.stderr
    assert Counter(executed.read_text(encoding="utf-8").splitlines()) == Counter(select(list(paths), 0, 4))


def test_ci_unit_shards_extra_and_empty_shards_exit_zero_without_starting_pytest(tmp_path):
    select = _select()
    paths = ["tests/test_one.py", "tests/nested/test_two.py"]
    for relative in paths:
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("this is not valid Python!\n", encoding="utf-8")
    parts = [select(list(paths), shard, 5) for shard in range(5)]
    assert Counter(path for part in parts for path in part) == Counter(paths)
    assert sum(bool(part) for part in parts) == 2, "Two files leave exactly three extra shards empty"
    for shard, part in enumerate(parts):
        if part:
            continue
        assert part == []
        result = _run_cli(tmp_path, shard, 5)
        assert result.returncode == 0, "An empty shard must succeed:\n" + result.stdout + result.stderr
        assert not (tmp_path / "pytest-started").exists(), "Empty argv must never fall back to full-suite pytest"
    for relative in paths:
        (tmp_path / relative).unlink()
    assert select([], 0, 1) == []
    result = _run_cli(tmp_path, 0, 1)
    assert result.returncode == 0, result.stdout + result.stderr
    assert not (tmp_path / "pytest-started").exists()
