"""Acceptance test for tick-config-from-base. Locked by Foragent; do not edit."""

from datetime import timedelta
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest
import yaml

from conftest import git_command
from foragent import cli
from foragent.capacity import write_state
from foragent.config import load_config
from test_orch_review_handoff import bind_cli
from test_scheduler import BODY, approve, ctx  # noqa: F401 (fixture)
from test_skills_autosync import cli_exit, isolated_home, write_global_config  # noqa: F401 (fixture)
from test_state_split import offline_acceptance  # noqa: F401 (fixture)


def _config(overlap):
    return yaml.safe_dump({"repo": "owner/repo", "overlap_per_file": overlap})


@pytest.fixture(autouse=True)
def local_git_only(tmp_path, monkeypatch, offline_acceptance):
    """Keep subprocesses inside the fixture; no runners, shells or real GitHub."""
    popen = subprocess.Popen

    def local_git(argv, *args, **kwargs):
        assert isinstance(argv, (list, tuple)) and argv[0] == "git", argv
        assert Path(kwargs["cwd"]).resolve().is_relative_to(tmp_path.resolve()), kwargs
        return popen(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", local_git)
    monkeypatch.setenv("FORAGENT_VIEW", "off")


@pytest.fixture(name="git_history")
def configured_history(git_history, tmp_path):
    # Extend the shared history fixture with a clean checkout that already has
    # committed config. Later updates import into the bare origin only.
    git_history.update({".foragent.yml": _config(2)})
    clone = tmp_path / "gate-checkout"
    git_command(tmp_path, "clone", str(git_history.origin), str(clone))
    git_history.clone = clone
    return git_history


@pytest.fixture
def tick_case(ctx, git_history, monkeypatch, isolated_home):
    # These settings exist only in HOME. Admission requires the inherited
    # approver, while the inherited ceiling leaves two new run slots.
    # The repository's overlap value must also override the global value.
    write_global_config({
        "approvers": ["human"], "max_parallel": 4, "overlap_per_file": 1,
        "skill_libraries": [], "notify": "none", "test_command": "true",
        "language": "en",
    })
    case = SimpleNamespace(
        ctx=ctx, root=ctx.repo_root, git=ctx.git, history=git_history,
        tracker=ctx.tracker._tracker, store=ctx.store, clock=[ctx.now()], starts=[],
    )
    case.main = bind_cli(case, monkeypatch)
    # bind_cli normally freezes config; this acceptance must exercise the real
    # loader, scheduler, fetch and admission decisions through fa gate tick.
    monkeypatch.setattr(cli, "load_config", load_config)
    (case.root / ".foragent.yml").write_text(_config(2), encoding="utf-8")
    assert git_command(case.root, "status", "--porcelain") == ""

    def launch(number):
        case.starts.append(number)
        return 7000 + number  # ctx supplies birth evidence in its fake process table.

    def unexpected(*args, **kwargs):
        pytest.fail("Config admission tests must not launch baseline processes")

    monkeypatch.setattr(cli, "spawn_runner", lambda *args: launch)
    monkeypatch.setattr(cli, "spawn_notify", lambda *args: None)
    monkeypatch.setattr(cli, "start_baseline_process", unexpected)
    write_state(case.store, 5, case.clock[0])
    for number in (1, 2, 3, 4):
        path = "src/shared.py" if number != 4 else "src/unrelated.py"
        approve(ctx, number, body=BODY + f"\n## Files\n- {path}\n",
                labels=["running" if number < 3 else "ready"])
        if number < 3:
            case.store.start_run(number, branch=f"foragent/issue-{number}",
                                 worktree=case.store.worktree_path(number), pid=7000 + number)
    return case


def _tick(case, capsys, *, no_start=False):
    head = case.git.ref_sha("HEAD")
    local = (case.root / ".foragent.yml").read_bytes()
    case.clock[0] += timedelta(minutes=5)
    capsys.readouterr()
    code = cli_exit(case.main, ["gate", "tick", "--json", "--repo-root", str(case.root),
                               *(["--no-start"] if no_start else [])])
    output = capsys.readouterr()
    assert code == 0, output.err
    report = json.loads(output.out)
    assert report["skipped"] is None, report
    assert case.git.ref_sha("origin/main") == case.history.tip, "The tick must fetch this origin update"
    assert case.git.ref_sha("HEAD") == head, "Reading config must not advance the shared checkout"
    assert (case.root / ".foragent.yml").read_bytes() == local, "Reading config must preserve local edits"
    assert not case.ctx.process_table.signals, "Healthy fixture runners must stay alive"
    return output, report


def _assert_admission(case, started, waiting, report):
    # Which config applies decides whether #3 (sharing src/shared.py with the
    # two running runs) may start; start order between #3 and #4 is not checked.
    started, waiting = set(started), set(waiting)
    assert set(case.starts) == started, report
    assert {run.number for run in case.store.active_runs()} == {1, 2} | started
    for number in started:
        assert "running" in case.tracker.get_issue(number).labels
        assert "ready" not in case.tracker.get_issue(number).labels
    for number in waiting:
        assert "ready" in case.tracker.get_issue(number).labels
        assert "running" not in case.tracker.get_issue(number).labels
        assert case.store.get_run(number) is None


def _assert_fallback(output):
    lines = output.err.splitlines()
    assert len(lines) == 1, f"Config fallback must emit one stderr line, got {output.err!r}"
    warning = lines[0]
    assert "origin/main" in warning and ".foragent.yml" in warning, warning
    assert "local" in warning.lower() or "working tree" in warning.lower(), (
        f"The diagnostic must explain that the tick is using the local config: {warning}"
    )


def test_tick_config_from_base_origin_update_applies_on_next_tick_without_pull(tick_case, capsys):
    case = tick_case
    output, _ = _tick(case, capsys, no_start=True)
    assert case.starts == [] and output.err == ""
    previous = case.git.ref_sha("origin/main")
    case.history.update({".foragent.yml": _config(3)})
    assert case.git.ref_sha("origin/main") == previous != case.history.tip
    assert git_command(case.root, "show", "HEAD:.foragent.yml") == _config(2).strip()

    output, report = _tick(case, capsys)

    _assert_admission(case, started={3, 4}, waiting=set(), report=report)
    assert output.err == ""
    assert git_command(case.root, "status", "--porcelain") == ""


def test_tick_config_from_base_ignores_uncommitted_local_config(tick_case, capsys):
    case = tick_case
    (case.root / ".foragent.yml").write_text(_config(3), encoding="utf-8")
    assert git_command(case.root, "diff", "--name-only") == ".foragent.yml"
    assert git_command(case.history.origin, "show", "main:.foragent.yml") == _config(2).strip()

    output, report = _tick(case, capsys)

    _assert_admission(case, started={4}, waiting={3}, report=report)
    assert output.err == ""
    assert git_command(case.root, "diff", "--name-only") == ".foragent.yml"


def test_tick_config_from_base_invalid_origin_yaml_uses_local_and_warns(tick_case, capsys):
    case = tick_case
    (case.root / ".foragent.yml").write_text(_config(3), encoding="utf-8")
    case.history.update({".foragent.yml": "overlap_per_file: [\n"})

    output, report = _tick(case, capsys)

    _assert_admission(case, started={3, 4}, waiting=set(), report=report)
    _assert_fallback(output)


def test_tick_config_from_base_missing_origin_config_uses_local_and_warns(tick_case, capsys):
    case = tick_case
    (case.root / ".foragent.yml").write_text(_config(3), encoding="utf-8")
    case.history.update({".foragent.yml": None})

    output, report = _tick(case, capsys)

    _assert_admission(case, started={3, 4}, waiting=set(), report=report)
    _assert_fallback(output)
