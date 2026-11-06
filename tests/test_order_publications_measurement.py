"""Publication red evidence comes from real pytest reports, never terminal summaries."""

import json
import shlex
import sys
from types import SimpleNamespace

import pytest

from foragent import order_prepare_check, order_scratch


def measure(tmp_path, monkeypatch, source, command=None, *, collect=True):
    def build(git, base, tree, context):
        tree.mkdir()
        (tree / "test_red.py").write_text(source)
        return order_scratch._entries(tree)

    monkeypatch.setattr(order_scratch, "build", build)
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    monkeypatch.delenv("PYTEST_PLUGINS", raising=False)
    directory = tmp_path / "measure"
    directory.mkdir()
    command = command or f"{shlex.quote(sys.executable)} -m pytest -p no:xdist -p no:cacheprovider -q test_red.py"
    results = order_prepare_check.run(None, "base", None, lambda tree: None,
                                      lambda *args: [], [command], directory, 30,
                                      collect_node_ids=collect)
    assert not (directory / "check").exists()
    return results[0], directory


def test_records_exact_parametrized_node_ids_without_terminal_interpretation(tmp_path, monkeypatch):
    source = ("import pytest\n"
              "@pytest.mark.parametrize('value', [1, 2], ids=['first', 'with space'])\n"
              "def test_red(value):\n"
              "    print('PASSED test_red.py::invented')\n"
              "    assert False\n")
    result, directory = measure(tmp_path, monkeypatch, source)
    expected = ["test_red.py::test_red[first]", "test_red.py::test_red[with space]"]
    assert result["exit_code"] == 1
    assert result["failing_node_ids"] == result["collected_node_ids"] == expected
    assert result["nonred_node_ids"] == []
    assert "invented" in result["output"]
    assert list(directory.glob("pytest-reports-*/*.json"))


def test_passing_and_xfail_tests_are_not_recorded_as_red(tmp_path, monkeypatch):
    source = ("import pytest\n"
              "def test_red(): assert False\n"
              "def test_green(): assert True\n"
              "@pytest.mark.xfail\n"
              "def test_expected(): assert False\n")
    result, _ = measure(tmp_path, monkeypatch, source)
    assert result["failing_node_ids"] == ["test_red.py::test_red"]
    assert result["nonred_node_ids"] == ["test_red.py::test_expected", "test_red.py::test_green"]
    assert result["collected_node_ids"] == ["test_red.py::test_expected", "test_red.py::test_green",
                                            "test_red.py::test_red"]


@pytest.mark.parametrize("source", [
    "invalid syntax here\n",
    "import pytest\n@pytest.fixture(autouse=True)\ndef broken(): assert False\ndef test_red(): assert False\n",
    "import pytest\n@pytest.fixture(autouse=True)\ndef broken():\n    yield\n    assert False\ndef test_red(): assert False\n",
])
def test_collection_setup_and_teardown_failures_are_unavailable(tmp_path, monkeypatch, source):
    result, _ = measure(tmp_path, monkeypatch, source)
    assert result["exit_code"] in (1, 2)
    assert result["failing_node_ids"] is None
    assert result["collected_node_ids"] is None
    assert result["nonred_node_ids"] is None


def test_nonpytest_command_cannot_forge_node_ids_in_output(tmp_path, monkeypatch):
    result, _ = measure(tmp_path, monkeypatch, "def test_red(): assert False\n",
                        "echo 'FAILED test_red.py::test_red'; exit 1")
    assert result["exit_code"] == 1
    assert result["failing_node_ids"] is None


def test_early_stopping_does_not_prove_unrun_tests_are_red(tmp_path, monkeypatch):
    command = f"{shlex.quote(sys.executable)} -m pytest -p no:xdist -p no:cacheprovider -x test_red.py"
    result, _ = measure(tmp_path, monkeypatch,
                        "def test_first(): assert False\ndef test_unrun(): assert False\n", command)
    assert result["exit_code"] == 1
    assert result["failing_node_ids"] is None


def test_multiple_pytest_sessions_in_one_command_keep_both_sets_of_facts(tmp_path, monkeypatch):
    invocation = f"{shlex.quote(sys.executable)} -m pytest -p no:xdist -p no:cacheprovider -q"
    command = f"{invocation} test_red.py::test_first; {invocation} test_red.py::test_second"
    result, directory = measure(tmp_path, monkeypatch,
                                "def test_first(): assert False\ndef test_second(): assert False\n", command)
    assert result["failing_node_ids"] == ["test_red.py::test_first", "test_red.py::test_second"]
    assert len(list(directory.glob("pytest-reports-*/*.json"))) == 2


def test_default_prepare_measurement_does_not_add_publication_reporting(tmp_path, monkeypatch):
    result, directory = measure(tmp_path, monkeypatch, "def test_red(): assert False\n", collect=False)
    assert result["exit_code"] == 1
    assert "failing_node_ids" not in result
    assert not (directory / "pytest-reporter").exists()


def test_parent_pytest_configuration_does_not_prefix_measured_node_ids(tmp_path, monkeypatch):
    (tmp_path / "pytest.ini").write_text("[pytest]\n")
    result, _ = measure(tmp_path, monkeypatch, "def test_red(): assert False\n")
    assert result["failing_node_ids"] == result["collected_node_ids"] == ["test_red.py::test_red"]


def test_command_pythonpath_assignment_keeps_reporter_and_project_configuration(tmp_path, monkeypatch):
    def build(git, base, tree, context):
        tree.mkdir()
        (tree / "src").mkdir()
        (tree / "src/parameters.py").write_text("VALUE = 'from-project'\n")
        (tree / "pytest.ini").write_text("[pytest]\npythonpath = helpers\n")
        (tree / "helpers").mkdir()
        (tree / "helpers/project_helper.py").write_text("VALUE = 'from-configuration'\n")
        test = tree / "tests/acceptance/test_new.py"
        test.parent.mkdir(parents=True)
        test.write_text("import parameters, project_helper\n"
                        "def test_new():\n"
                        "    assert (parameters.VALUE, project_helper.VALUE) == ('missing', 'missing')\n")
        return order_scratch._entries(tree)

    monkeypatch.setattr(order_scratch, "build", build)
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    command = (f"PYTHONPATH=src {shlex.quote(sys.executable)} -m pytest -p no:xdist "
               "-p no:cacheprovider tests/acceptance/test_new.py")
    results = order_prepare_check.run(None, "base", None, lambda tree: None, lambda *args: [],
                                     [command], tmp_path, 30, collect_node_ids=True)
    assert results[0]["exit_code"] == 1, results[0]["output"]
    assert results[0]["failing_node_ids"] == ["tests/acceptance/test_new.py::test_new"]
    assert results[0]["collected_node_ids"] == results[0]["failing_node_ids"]


def reporter(monkeypatch, directory, *, worker=False):
    monkeypatch.setenv("FORAGENT_RED_REPORTS", str(directory))
    monkeypatch.setenv("FORAGENT_RED_TREE", str(directory))
    hooks = {}
    exec(compile(order_prepare_check._NODE_REPORTER, "publication-reporter", "exec"), hooks)
    config = SimpleNamespace(rootpath=directory, **({"workerinput": {}} if worker else {}))
    session = SimpleNamespace(config=config)
    hooks["pytest_sessionstart"](session)
    return hooks, session


def failed_call(hooks, session, node):
    report = SimpleNamespace(nodeid=node, when="call", outcome="failed", failed=True)
    hook = hooks["pytest_runtest_makereport"](SimpleNamespace(session=session),
                                             SimpleNamespace(excinfo=object()))
    next(hook)
    with pytest.raises(StopIteration):
        hook.send(SimpleNamespace(get_result=lambda: report))
    # The xdist transport carries the custom failure fact with the pytest report.
    return SimpleNamespace(**json.loads(json.dumps(vars(report))))


def test_distributed_reports_use_complete_controller_outcomes_and_worker_collection(tmp_path, monkeypatch):
    worker, worker_session = reporter(monkeypatch, tmp_path, worker=True)
    nodes = ["test_red.py::test_first", "test_red.py::test_second"]
    worker["pytest_collection_finish"](SimpleNamespace(
        _foragent_red=worker_session._foragent_red,
        items=[SimpleNamespace(nodeid=node, path=tmp_path / node.split("::", 1)[0]) for node in nodes]))
    first = failed_call(worker, worker_session, nodes[0])
    worker["pytest_runtest_logreport"](first)
    worker["pytest_sessionfinish"](worker_session, 1)
    assert not list(tmp_path.iterdir()), "A worker's partial run cannot become a complete measurement"

    controller, session = reporter(monkeypatch, tmp_path)
    controller["pytest_xdist_node_collection_finished"](SimpleNamespace(), nodes)
    controller["pytest_xdist_node_collection_finished"](SimpleNamespace(), nodes)
    controller["pytest_runtest_logreport"](first)
    controller["pytest_runtest_logreport"](failed_call(worker, worker_session, nodes[1]))
    controller["pytest_sessionfinish"](session, 1)
    assert order_prepare_check._node_facts(tmp_path) == {
        "failing_node_ids": nodes, "collected_node_ids": nodes, "nonred_node_ids": []}


def test_distributed_workers_with_different_collection_cannot_prove_red(tmp_path, monkeypatch):
    hooks, session = reporter(monkeypatch, tmp_path)
    first, second = "test_red.py::test_first", "test_red.py::test_second"
    hooks["pytest_xdist_node_collection_finished"](SimpleNamespace(), [first])
    hooks["pytest_xdist_node_collection_finished"](SimpleNamespace(), [first, second])
    hooks["pytest_runtest_logreport"](failed_call(hooks, session, first))
    hooks["pytest_sessionfinish"](session, 1)
    assert order_prepare_check._node_facts(tmp_path)["failing_node_ids"] is None
