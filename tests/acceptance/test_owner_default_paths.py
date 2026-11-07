"""More acceptance checks for owner-default (q58 A, 2026-11-07).

tests/acceptance/test_owner_default.py is locked and stays unchanged. This file
adds paths it does not cover:

- the committed (origin) .foragent.yml that has no merge_owner key, which the
  gate reads each tick, also runs with the merge owner and batch merge on;
- a checks: local project without the key still loads and runs the owner;
- the old merge-turn path (merge_group_max: 1) and the old merge-group path
  (merge_group_max > 1) with merge_owner false never let the worker merge;
- the gate-start message does not stop the gate, and an explicit
  merge_owner: true gets no message.

Public entry points only: the settings loaders, ``fa gate tick`` through
``foragent.cli.main``, the worker's ``run_issue`` and the public owner pass
``merge_owner.tick``. GitHub, models and the clock are fakes; no real process
is signalled.
"""

import yaml

from foragent.config import load_committed_config, load_config
from test_ci_feedback import make_case
from test_owner_default import PYTEST, _owner_only_message, _worker_hands_off, gate_case  # noqa: F401
from test_scheduler import ctx  # noqa: F401 (fixture)
from test_skills_autosync import cli_exit, isolated_home  # noqa: F401 (fixture)
from test_state_split import offline_acceptance  # noqa: F401 (fixture)

from datetime import timedelta


# --- Statement 1: a config without merge_owner runs with the owner and batch merge on.

def _project(tmp_path, settings):
    root = tmp_path / "project"
    root.mkdir(exist_ok=True)
    (root / ".foragent.yml").write_text(yaml.safe_dump({"repo": "owner/repo", **settings}))
    return root


def test_committed_config_without_merge_owner_runs_owner_and_batch_merge(tmp_path):
    global_path = tmp_path / "missing-global.yml"
    root = _project(tmp_path, {"checks": "ci", "test_command": PYTEST})
    local = load_config(root, global_path)
    origin_text = yaml.safe_dump({"repo": "owner/repo", "checks": "ci", "test_command": PYTEST,
                                  "max_retries": 3})
    committed, ignored = load_committed_config(local, origin_text, "origin/main:.foragent.yml", global_path)
    assert committed is not None, "The committed config without merge_owner must load, not be refused"
    assert ignored == []
    assert committed.max_retries == 3, "Setup: the committed config's own values apply"
    assert committed.merge_owner is True, "A committed config without merge_owner must run the merge owner"
    assert committed.merge_batch is True, "A committed config without merge_owner must run batch merge"


def test_local_checks_project_without_merge_owner_key_loads_and_runs_owner(tmp_path):
    # Batch merge needs CI evidence; this check only pins that the project is not refused
    # and that the merge owner (the only merge path) is on.
    root = _project(tmp_path, {"checks": "local", "test_command": PYTEST})
    config = load_config(root, tmp_path / "missing-global.yml")
    assert config.merge_owner is True, "A checks: local project without merge_owner must run the merge owner"


def test_global_config_without_merge_owner_does_not_turn_owner_off(tmp_path):
    global_path = tmp_path / "global.yml"
    global_path.write_text(yaml.safe_dump({"language": "en", "notify": "none"}))
    root = _project(tmp_path, {"checks": "ci", "test_command": PYTEST})
    config = load_config(root, global_path)
    assert config.merge_owner is True
    assert config.merge_batch is True


# --- Statement 2: merge_owner false gets a message at gate start; the gate still runs.

def _gate_tick(case, capsys, settings):
    text = yaml.safe_dump({"repo": "owner/repo", "test_command": PYTEST, **settings})
    (case.root / ".foragent.yml").write_text(text)
    case.clock[0] += timedelta(minutes=5)
    capsys.readouterr()
    code = cli_exit(case.main, ["gate", "tick", "--repo-root", str(case.root)])
    output = capsys.readouterr()
    return code, output.out + "\n" + output.err


def test_merge_owner_false_message_does_not_stop_the_gate(gate_case, capsys):
    quiet_code, quiet = _gate_tick(gate_case, capsys, {})
    code, output = _gate_tick(gate_case, capsys, {"merge_owner": False})
    lines = _owner_only_message(output)
    assert lines, f"Gate start must name merge_owner and say the merge owner is the only merge path:\n{output}"
    assert code == quiet_code, (
        f"The message must not change how the gate tick ends ({quiet_code} without the key, {code} with it):\n"
        f"{output}")


def test_explicit_merge_owner_true_gets_no_message(gate_case, capsys):
    _code, output = _gate_tick(gate_case, capsys, {"merge_owner": True})
    assert "only merge path" not in " ".join(output.lower().split()), (
        f"Only merge_owner: false gets the message:\n{output}")
    # Wrong-value guard: the same gate on merge_owner false does say it.
    _code, output = _gate_tick(gate_case, capsys, {"merge_owner": False})
    assert _owner_only_message(output), f"merge_owner false must get the message:\n{output}"


# --- Statement 3: no worker merge turn or merge group; the reviewed PR goes to the owner.

def test_old_merge_turn_settings_worker_hands_pr_to_owner(git_history, tmp_path, monkeypatch):
    # merge_group_max 1 turned groups off: the old worker merge-turn path.
    case = make_case(git_history, tmp_path, monkeypatch, test_command=PYTEST, checks="local",
                     merge_owner=False, merge_batch=False, merge_group_max=1)
    _worker_hands_off(case)


def test_old_merge_group_settings_worker_hands_pr_to_owner(git_history, tmp_path, monkeypatch):
    # merge_group_max above 1: the old worker merge-group path.
    case = make_case(git_history, tmp_path, monkeypatch, test_command=PYTEST, checks="local",
                     merge_owner=False, merge_batch=False, merge_group_max=4)
    _worker_hands_off(case)
