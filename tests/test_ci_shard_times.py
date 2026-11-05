import json
from pathlib import Path
import runpy

from foragent import ci_pytest, ci_shard

SCRIPT = Path(__file__).parents[1] / "scripts/shard_times.py"


def test_balanced_puts_longest_first_on_least_loaded_shard():
    times = {"a.py": 10, "b.py": 7, "c.py": 6, "d.py": 4, "e.py": 3}
    # 10 -> 0, 7 -> 1, 6 -> 1 (7 < 10), 4 -> 0 (10 < 13), 3 -> 1 (13 < 14)
    assert ci_pytest.balanced(times, 2) == {"a.py": 0, "b.py": 1, "c.py": 1, "d.py": 0, "e.py": 1}


def test_balanced_ties_go_by_path_then_lowest_shard():
    assert ci_pytest.balanced({"z.py": 1, "a.py": 1, "m.py": 1}, 3) == {"a.py": 0, "m.py": 1, "z.py": 2}


def test_unmeasured_file_keeps_the_path_hash(tmp_path):
    assignment = ci_pytest.balanced({"a.py": 5}, 4)
    assert ci_pytest.shard_of("a.py", 4, assignment) == 0
    hashed = {ci_pytest.shard_of("new.py", 4, assignment) for _ in range(3)}
    assert hashed == {ci_pytest.shard_of("new.py", 4, {})}


def test_unreadable_times_fall_back_to_hash(tmp_path):
    bad = tmp_path / "times.json"
    for text in ("not json", "[1]", '{"a.py": "x"}', '{"a.py": -1}', '{"a.py": true}'):
        bad.write_text(text)
        assert ci_pytest.read_times(str(bad)) == {}
    assert ci_pytest.read_times(str(tmp_path / "missing.json")) == {}
    assert ci_pytest.read_times(None) == {}
    bad.write_text('{"a.py": 2}')
    assert ci_pytest.read_times(str(bad)) == {"a.py": 2.0}


def test_base_times_keep_only_files_present_at_base(tmp_path):
    base, out = tmp_path / "base", tmp_path / "out"
    (base / "scripts").mkdir(parents=True)
    out.mkdir()
    (base / "t").mkdir()
    (base / "t/kept.py").write_text("")
    (base / ci_shard.TIMES).write_text(json.dumps({"t/kept.py": 3, "t/gone.py": 9}))
    copied = ci_shard.base_times(base, out)
    assert json.loads(copied.read_text()) == {"t/kept.py": 3}
    (base / ci_shard.TIMES).write_text("{broken")
    assert ci_shard.base_times(base, out) is None


def test_refresh_script_sums_testcase_time_per_file(tmp_path):
    junit = tmp_path / "junit.xml"
    junit.write_text(
        '<testsuites><testsuite>'
        '<testcase classname="tests.acceptance.test_a" name="x" file="tests/acceptance/test_a.py" time="1.24"/>'
        '<testcase classname="tests.acceptance.test_a" name="y" file="tests/acceptance/test_a.py" time="2.0"/>'
        '<testcase classname="tests.acceptance.test_b.TestCase" name="z" time="0.5"/>'
        '</testsuite></testsuites>')
    target = tmp_path / "times.json"
    script = runpy.run_path(str(SCRIPT))
    assert script["main"](["shard_times.py", str(junit), str(target)]) == 0
    assert json.loads(target.read_text()) == {"tests/acceptance/test_a.py": 3.2,
                                              "tests/acceptance/test_b.py": 0.5}
