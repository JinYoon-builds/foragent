"""The combined red check includes generated lane configuration that will land."""

from copy import deepcopy
import shlex
import sys

import yaml

from test_order_publications import case, submit  # noqa: F401
from foragent import order, order_prepare_check, order_prepare_cycle, order_prepare_review
from foragent import order_prepare_setup, order_publications, order_scratch
from foragent.scheduler import TickReport


_MEASURE = order_prepare_check.run


def test_combined_check_includes_another_requests_generated_lane_config(case, monkeypatch):
    settings = "repo: owner/repo\nmerge_owner: true\ntest_command: uv run pytest\n"
    (case.root / ".foragent.yml").write_text(settings)
    case.git.remote.snapshots["main-1"] = {".foragent.yml": settings, "pytest.ini": "[pytest]\n"}
    submit(case)
    first, = order_publications.pending(case.store)
    second = deepcopy(first)
    second.update(id="second", sequence=2, title="Observe API lane")
    second["plan"].update(id="observe-api", title="Observe API lane")
    first["plan"]["new_lanes"] = [{"name": "api", "paths": ["api/**"], "skills": []}]
    sources = ["def test_new():\n    assert 1 == 2\n",
               "from pathlib import Path\nimport yaml\n"
               "def test_new():\n"
               "    settings = yaml.safe_load(Path('.foragent.yml').read_text())\n"
               "    assert 'api' in (settings.get('lanes') or {})\n"]
    paths = ["tests/acceptance/test_new.py", "tests/acceptance/test_observe_api.py"]
    for row, name, source in zip((first, second), paths, sources, strict=True):
        command = f"{shlex.quote(sys.executable)} -m pytest -p no:xdist -p no:cacheprovider -q {name}"
        item = row["plan"]["issues"][0]
        item.update(acceptance_files={name: source}, done_when=[command])
        row["files"] = order.validate_plan(row["plan"], case.root, case.config)
        row["prepared"]["new"].update(
            written={name: source}, done_when=[command], failing_node_ids=[f"{name}::test_new"],
            cycle=order_prepare_cycle.identity(row["plan"], item, case.config, case.root, row["files"]),
            published=order_prepare_setup.published_digest(row["plan"], case.root, case.config, row["files"]))
        order_publications._save(case.store, row)

    def build(git, base, tree, context):
        tree.mkdir(parents=True)
        for name, data in git.remote.snapshots[base].items():
            destination = tree / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(data)
        context.apply(tree)
        return order_scratch._entries(tree)

    measured = []

    def run(*args, **kwargs):
        results = _MEASURE(*args, **kwargs)
        context, commands = args[2], args[5]
        staged = context.files.get(".foragent.yml", settings.encode())
        measured.append((commands[0], yaml.safe_load(staged).get("lanes", {}), results))
        return results

    monkeypatch.setattr(order_scratch, "build", build)
    monkeypatch.setattr(order_prepare_review, "_integrity", lambda *args: [])
    monkeypatch.setattr(order_prepare_check, "run", run)
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    monkeypatch.delenv("PYTEST_PLUGINS", raising=False)
    report = TickReport()
    order_publications.check_pending(case.ctx, report)

    rows = order_publications.requests(case.store)
    assert [row["status"] for row in rows] == ["pending", "pending"], [row["reason"] for row in rows]
    assert all(row["red_check_commit"] == "main-1" for row in rows)
    assert all(row["red_check_single_only"] for row in rows)
    assert all(not row.get("red_check_set") for row in rows)
    assert all(row["red_checks"]["new"]["measured"][0]["exit_code"] == 1 for row in rows)
    observed = [(lanes, results[0]["exit_code"]) for command, lanes, results in measured
                if paths[1] in command]
    assert observed == [({}, 1), ({"api": {"paths": ["api/**"], "agent": "fa-api", "skills": []}}, 0)]
    assert not case.events and not case.tracker.list_issues()
    assert "retry singly" in report.actions[-1]
