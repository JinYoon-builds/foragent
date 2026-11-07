"""Acceptance test for owner-default (q58 A, 2026-11-07).

The merge owner and batch merge are the product default. A project that still
writes ``merge_owner: false`` is told at gate start that the setting is no
longer supported and that the merge owner is the only merge path. No worker
merges its own PR: an approved issue's reviewed PR is handed to the merge
owner, whatever the project's config says.

Public entry points only: the settings loader, ``fa init`` and ``fa gate tick``
through ``foragent.cli.main``, the worker's ``run_issue`` and one public owner
pass (``merge_owner.tick``). The handoff is checked by its observable results
(who merged the PR, the run outcome), not by the owner's queue format. GitHub,
models and the clock are fakes; no real process is signalled.
"""

from datetime import timedelta
import shlex
import sys
from types import SimpleNamespace

import pytest
import yaml

from foragent import cli
from foragent.config import Config, load_config
from test_ci_feedback import make_case
from test_ci_mode import init_case
from test_orch_review_handoff import bind_cli
from test_scheduler import ctx  # noqa: F401 (fixture)
from test_skills_autosync import cli_exit, isolated_home, write_global_config  # noqa: F401 (fixture)
from test_state_split import offline_acceptance  # noqa: F401 (fixture)

PYTEST = f"{shlex.quote(sys.executable)} -m pytest -q"


# --- Statement 1: fa init and a config without merge_owner run with the owner and batch merge on.

def test_config_without_merge_owner_key_runs_owner_and_batch_merge(tmp_path):
    global_path = tmp_path / "missing-global.yml"
    root = tmp_path / "project"
    root.mkdir()
    (root / ".foragent.yml").write_text(yaml.safe_dump({
        "repo": "owner/repo", "checks": "ci", "test_command": PYTEST,
    }))
    config = load_config(root, global_path)
    assert config.merge_owner is True, "A config without merge_owner must run with the merge owner on"
    assert config.merge_batch is True, "A config without merge_owner must run with batch merge on"


def test_product_default_config_has_owner_and_batch_merge_on():
    config = Config()
    assert config.merge_owner is True, "The product default must be the merge owner"
    assert config.merge_batch is True, "The product default must be batch merge"


def test_fa_init_project_runs_owner_and_batch_merge(git_history, tmp_path, monkeypatch, capsys):
    # Public repository: init chooses checks: ci (the pytest command is known).
    case = init_case(git_history, tmp_path, monkeypatch, private=False)
    (case.root / ".foragent.yml").write_text(f"test_command: {PYTEST}\n")
    assert case.main(case.argv) == 0, capsys.readouterr().err
    written = yaml.safe_load((case.root / ".foragent.yml").read_text()) or {}
    assert written.get("merge_owner", True) is not False, "fa init must not write merge_owner: false"
    assert written.get("merge_batch", True) is not False, "fa init must not write merge_batch: false"
    config = load_config(case.root, tmp_path / "missing-global.yml")
    assert config.merge_owner is True, "A project created by fa init must run with the merge owner"
    assert config.merge_batch is True, "A project created by fa init must run with batch merge"


# --- Statement 2: merge_owner false gets a message at gate start.

@pytest.fixture
def gate_case(ctx, monkeypatch, isolated_home, offline_acceptance):  # noqa: F811
    write_global_config({"approvers": ["human"], "skill_libraries": [], "notify": "none",
                         "language": "en"})
    case = SimpleNamespace(ctx=ctx, root=ctx.repo_root, git=ctx.git, tracker=ctx.tracker._tracker,
                           store=ctx.store, clock=[ctx.now()])
    case.main = bind_cli(case, monkeypatch)
    monkeypatch.setattr(cli, "load_config", load_config)  # The real settings loader.
    monkeypatch.setattr(cli, "spawn_runner", lambda *args: (lambda number: 7000 + number))
    monkeypatch.setattr(cli, "spawn_notify", lambda *args: None)
    monkeypatch.setattr(cli, "start_baseline_process", lambda *args, **kwargs: None)
    monkeypatch.setenv("FORAGENT_VIEW", "off")
    return case


def _gate_start(case, capsys, settings):
    text = yaml.safe_dump({"repo": "owner/repo", "test_command": PYTEST, **settings})
    (case.root / ".foragent.yml").write_text(text)
    case.clock[0] += timedelta(minutes=5)
    capsys.readouterr()
    cli_exit(case.main, ["gate", "tick", "--repo-root", str(case.root)])
    output = capsys.readouterr()
    return output.out + "\n" + output.err


def _owner_only_message(output):
    """Lines that name merge_owner, when the output also says the merge owner is the only merge path."""
    flat = " ".join(output.lower().split())
    if "merge owner" not in flat or "only merge path" not in flat:
        return []
    return [line for line in output.splitlines() if "merge_owner" in line]


def test_merge_owner_false_gets_a_message_at_gate_start(gate_case, capsys):
    # Guard: a project without the setting gets no such message (a message for everyone fails).
    quiet = _gate_start(gate_case, capsys, {})
    assert not _owner_only_message(quiet), f"Only a project that sets merge_owner false gets the message:\n{quiet}"
    output = _gate_start(gate_case, capsys, {"merge_owner": False})
    lines = _owner_only_message(output)
    assert lines, ("Gate start must name the merge_owner setting and say the merge owner is now the only "
                   f"merge path; got:\n{output}")
    assert " ".join(output.lower().split()).count("only merge path") == 1, f"Say it once, not repeated:\n{output}"


# --- Statement 3: no worker merge turn or merge group; the reviewed PR goes to the merge owner.

def _worker_hands_off(case):
    from foragent import merge_owner
    from foragent.runner import run_issue
    from foragent.scheduler import TickReport

    outcome = run_issue(1, case.ctx)
    log = case.store.log_path(1).read_text() if case.store.log_path(1).exists() else ""
    assert case.reviewer.calls, "Setup: the PR was reviewed"
    assert not case.tracker.merged_prs, f"The worker must never merge its own PR (outcome {outcome}):\n{log}"
    assert outcome == "merge-queued", f"The worker hands the reviewed PR to the merge owner, got {outcome}:\n{log}"
    assert case.store.get_run(1).outcome == "merge-queued"
    # The handoff is real: one public owner pass lands the PR, exactly once.
    merge_owner.tick(case.ctx, TickReport())
    assert len(case.tracker.merged_prs) == 1, (
        f"The merge owner must land the handed-off PR once, got {case.tracker.merged_prs}:\n{log}")
    merge_owner.tick(case.ctx, TickReport())
    assert len(case.tracker.merged_prs) == 1, f"A second owner pass must not merge again: {case.tracker.merged_prs}"


def test_default_project_worker_hands_reviewed_pr_to_merge_owner(git_history, tmp_path, monkeypatch):
    # make_case builds the product Config without a merge_owner value.
    case = make_case(git_history, tmp_path, monkeypatch, test_command=PYTEST, checks="local")
    _worker_hands_off(case)


def test_merge_owner_false_project_worker_still_never_merges(git_history, tmp_path, monkeypatch):
    case = make_case(git_history, tmp_path, monkeypatch, test_command=PYTEST, checks="local",
                     merge_owner=False, merge_batch=False)
    _worker_hands_off(case)
