"""Offline native Stop hook checks, using approved snapshots and the real check runner."""

from pathlib import Path
import shlex
import sys

import pytest

from foragent.cli import main
from foragent.issue import body_hash
from foragent.runner import CheckResult
from foragent.state import StateStore


@pytest.fixture
def hook(tmp_path):
    store = StateStore(tmp_path / "state with spaces")
    worktree = tmp_path / "work tree"
    worktree.mkdir()

    def setup(commands):
        body = "<!-- foragent:v1 -->\n## Goal\nFeature\n## Out of scope\nOther\n## Done when\n"
        body += "\n".join(f"```sh\n{command}\n```" for command in commands)
        store.save_approval(7, body=body, body_sha=body_hash(body), acceptance_blobs={},
                            approved_by="human", approved_at="2026-10-20T00:00:00+00:00")
        return ["gate", "check-done", "--snapshot", str(store.root / "approvals/7.json"),
                "--worktree", str(worktree), "--acceptance-dir", "tests/acceptance/"]

    return setup, worktree, store.root / "runs/issue-7/stop-block-count"


def test_check_done_runs_all_commands_from_root_with_scrubbed_environment(hook, monkeypatch, capsys):
    setup, worktree, counter = hook
    for key in ("VIRTUAL_ENV", "VIRTUAL_ENV_PROMPT", "PYTHONHOME", "UV_PROJECT_ENVIRONMENT"):
        monkeypatch.setenv(key, "gate environment")
    args = setup([
        'test -z "${VIRTUAL_ENV}${VIRTUAL_ENV_PROMPT}${PYTHONHOME}${UV_PROJECT_ENVIRONMENT}"',
        "printf 'root output' > checked.txt", "test -f checked.txt",
    ])
    from foragent.runner import run_logged
    calls = []

    def logged(argv, **kwargs):
        calls.append((argv, kwargs))
        return run_logged(argv, **kwargs)

    monkeypatch.setattr("foragent.runner.run_logged", logged)
    assert main(args) == 0
    assert (worktree / "checked.txt").read_text() == "root output"
    assert len(calls) == 3
    assert all(kwargs["cwd"] == worktree and kwargs["timeout_s"] == 900 for _, kwargs in calls)
    output = capsys.readouterr()
    assert len(output.out.splitlines()) == 1 and "all 3 commands passed" in output.out
    assert output.err == "" and not counter.exists()


def test_check_done_blocks_and_reports_only_last_2000_output_characters(hook, capsys):
    setup, worktree, counter = hook
    command = shlex.join([sys.executable, "-c", "print('lost prefix' + 'x' * 2100 + 'tail'); raise SystemExit(4)"])
    second = "echo second-failure; exit 3"
    assert main(setup([command, "touch still-runs", second])) == 2
    output = capsys.readouterr()
    assert output.out == ""
    # Every failing command is reported; the 2000-character limit names what it left out (#420).
    full = "lost prefix" + "x" * 2100 + "tail\n"
    saved = counter.parent / "stop-check-0.log"
    assert output.err.startswith(f"Done when failed: {command}\n[cut: {len(full) - 2000} more chars; full text: {saved}]\n"
                                 + full[-2000:] + "\n\nDone when failed: " + second + "\nsecond-failure\n")
    assert saved.read_text() == full
    assert counter.read_text() == "1" and (worktree / "still-runs").exists()


def test_check_done_blocks_eight_times_then_skips_checks(hook, capsys):
    setup, worktree, counter = hook
    args = setup(["printf x >> tries; exit 1"])
    for count in range(1, 9):
        assert main(args) == 2
        assert counter.read_text() == str(count)
    assert main(args) == main(args) == 0
    assert (worktree / "tries").read_text() == "x" * 8
    assert counter.read_text() == "8"
    assert "cap reached" in capsys.readouterr().out


def test_success_does_not_reset_cumulative_block_count(hook):
    setup, _, counter = hook
    assert main(setup(["exit 1"])) == 2
    assert main(setup(["exit 0"])) == 0
    assert counter.read_text() == "1"


@pytest.mark.parametrize("failure", ["timeout", "missing", "invalid", "no-command"])
def test_check_done_errors_block_stop(hook, monkeypatch, capsys, failure):
    setup, _, counter = hook
    args = setup(["exit 0"] if failure != "no-command" else [])
    if failure == "timeout":
        monkeypatch.setattr("foragent.runner._check", lambda *args, **kwargs: CheckResult(args[0], 0, True, "slow output"))
    elif failure in {"missing", "invalid"}:
        path = Path(args[args.index("--snapshot") + 1])
        if failure == "missing":
            path.unlink()
        else:
            path.write_text("not JSON")
    assert main(args) == 2
    assert counter.read_text() == "1"
    error = capsys.readouterr().err
    assert "Done when failed:" in error
    if failure == "timeout":
        assert "exit 0" in error and "slow output" in error and "timed out" in error


def test_check_done_launch_error_still_reports_earlier_failures(hook, capsys, monkeypatch):
    """#420: an error on a later command does not hide the failures gathered before it."""
    from foragent import runner
    from foragent.run_checks import CheckResult

    setup, worktree, counter = hook
    args = setup(["check-a", "check-b"])

    def check(command, *rest, **kwargs):
        if command == "check-b":
            raise OSError("cannot launch check-b")
        return CheckResult(command, 1, False, "A failed output")

    monkeypatch.setattr(runner, "_check", check)
    assert main(args) == 2
    err = capsys.readouterr().err
    assert "Done when failed: check-a\nA failed output" in err and "cannot launch check-b" in err
