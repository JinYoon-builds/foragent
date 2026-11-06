"""Added-failure exceptions are proved on the immutable prepare base and plan bytes."""

import shlex
import sys
from contextlib import contextmanager
from copy import deepcopy
from types import SimpleNamespace

import pytest

from foragent import order, order_collect, order_prepare, order_prepare_cycle, order_publications
from foragent.config import Config
from foragent.git import GitRepo
from foragent.state import StateStore


NAME = "tests/acceptance/test_plan.py"


@pytest.fixture
def facts_case(git_history, tmp_path, monkeypatch):
    monkeypatch.delenv("PYTHONPATH", raising=False)
    monkeypatch.delenv("PYTEST_PLUGINS", raising=False)
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    config = Config(test_command=f"{shlex.quote(sys.executable)} -m pytest -p no:xdist", language="en")
    return git_history, GitRepo(git_history.clone), config, tmp_path / "added-facts"


def added(facts_case, base, files, nodes):
    _history, git, config, directory = facts_case
    git.fetch("origin", "main")
    return order_publications._plan_added_ids(git, base, files, set(nodes), directory, config)


def test_added_test_in_existing_file_is_proved_by_collection_difference(facts_case):
    history, *_ = facts_case
    source = "def test_existing(): assert False\n"
    history.update({NAME: source})
    base = history.tip
    plan = source + "def test_added(): assert False\n"
    assert added(facts_case, base, {NAME: plan},
                 {f"{NAME}::test_existing", f"{NAME}::test_added"}) == {f"{NAME}::test_added"}


def test_main_only_parameter_addition_is_not_a_test_added_by_the_plan(facts_case):
    history, *_ = facts_case
    data = "tests/acceptance/parameters.json"
    source = ("import json\nfrom pathlib import Path\nimport pytest\n"
              "VALUES = json.loads(Path(__file__).with_name('parameters.json').read_text())\n"
              "@pytest.mark.parametrize('value', VALUES)\n"
              "def test_existing(value): assert False\n")
    history.update({NAME: source, data: '["base"]\n'})
    base = history.tip
    plan = source + "def test_added(): assert False\n"
    # The carried test file is unchanged, but mutable main now generates another node.
    history.update({data: '["base", "main-only"]\n'})
    assert added(facts_case, base, {NAME: plan},
                 {f"{NAME}::test_existing[main-only]", f"{NAME}::test_added"}) == {f"{NAME}::test_added"}


def test_new_file_accepts_only_node_ids_collected_from_this_plan(facts_case):
    history, *_ = facts_case
    base = history.tip
    assert added(facts_case, base, {NAME: "def test_added(): assert False\n"},
                 {f"{NAME}::test_added", f"{NAME}::test_fabricated"}) == {f"{NAME}::test_added"}


def test_first_check_seeds_reviewed_parameters_before_target_removal(facts_case, monkeypatch):
    history, git, config, _directory = facts_case
    data = "tests/acceptance/parameters.json"
    history.update({data: '["first", "second"]\n'})
    prepared_base = history.tip
    source = ("import json\nfrom pathlib import Path\nimport pytest\n"
              "VALUES = json.loads(Path(__file__).with_name('parameters.json').read_text())\n"
              "@pytest.mark.parametrize('value', VALUES)\n"
              "def test_existing(value): assert value == 'implemented'\n")
    command = f"{config.test_command} -p no:cacheprovider {NAME}"
    item = {"key": "new", "lane": "main", "done_when": [command], "acceptance_files": {NAME: source}}
    plan = {"id": "parametrized", "issues": [item]}
    admitted = {"key": "new", "status": "reviewed", "cycle": "reviewed-cycle", "attempts": [],
                "base_revision": prepared_base, "written": {NAME: source}, "done_when": [command]}
    store = StateStore.for_repo("owner/repo")
    row = {"id": "request", "sequence": 1, "repo": "owner/repo", "status": "pending", "target": "main",
           "title": "Parametrized", "plan": plan, "prepared": {"new": admitted}, "files": {NAME: source},
           "approve": False, "approve_keys": [], "approve_numbers": []}
    order_publications._save(store, row)
    path = order_prepare._record_path(store, plan)
    path.parent.mkdir(parents=True, exist_ok=True)
    order_prepare._write(path, {"plan": plan["id"], "issues": {"new": deepcopy(admitted)}})
    history.update({data: '["first"]\n'})
    target = history.tip
    git.fetch("origin", "main")
    monkeypatch.setattr(git, "head_sha", lambda: target)
    publication = SimpleNamespace(files={NAME: source}, by_number={})
    monkeypatch.setattr(order, "_publication_checks", lambda *args, **kwargs: publication)
    monkeypatch.setattr(order_collect, "check", lambda *args, **kwargs: None)

    @contextmanager
    def checkout(*args):
        yield history.clone, git

    monkeypatch.setattr(order_publications, "_checkout", checkout)
    ctx = SimpleNamespace(store=store, config=config, repo="owner/repo", tracker=None, git=git)
    order_publications._check_request(ctx, "request", target)
    returned, = order_publications.requests(store)
    assert returned["status"] == "preparation", returned.get("reason")
    assert returned["baseline_failing_node_ids"]["new"] == [
        f"{NAME}::test_existing[first]", f"{NAME}::test_existing[second]"]
    assert returned["red_checks"]["new"]["failing_node_ids"] == [f"{NAME}::test_existing[first]"]
    assert f"{NAME}::test_existing[second]" in returned["reason"]
    entry = order_prepare._read(path, plan)["issues"]["new"]
    assert returned["reason"].removeprefix("new: ") in order_prepare_cycle.revision_notes(entry)


@pytest.mark.parametrize("failure", ["missing-report", "unavailable"])
def test_unavailable_collection_cannot_prove_an_added_failure(facts_case, monkeypatch, failure):
    history, *_ = facts_case
    source = "def test_existing(): assert False\n"
    history.update({NAME: source})
    base = history.tip

    def unavailable(*args, **kwargs):
        if failure == "unavailable":
            raise order_collect._Unavailable("Cannot collect", None)
        return 0, None, None, "pytest"

    monkeypatch.setattr(order_collect, "run_copy", unavailable)
    with pytest.raises(order_publications.RedCheckFailed, match="Could not establish added test node ids"):
        added(facts_case, base, {NAME: source + "def test_added(): assert False\n"}, {f"{NAME}::test_added"})
