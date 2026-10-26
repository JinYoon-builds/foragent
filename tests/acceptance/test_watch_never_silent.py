"""Acceptance test for watch-never-silent. Locked by Foragent; do not edit."""

import inspect
import json

import pytest

from test_local_truth import local_cli, record_gh, snapshot_case


def fail_first_read(case, monkeypatch, message="state read failed: disk busy"):
    """Make the first watched-state read fail, whichever source the watcher uses."""
    from foragent.state import StateStore

    failures = [message]
    targets = [(case.tracker, "list_open_issues")]
    if callable(getattr(StateStore, "local_issues", None)):
        targets.append((StateStore, "local_issues"))
    for owner, name in targets:
        original = getattr(owner, name)

        def read(*args, _original=original, **kwargs):
            # Fail only the watcher's own read, not unrelated CLI start-up reads.
            if failures and any("/foragent/orch" in frame.filename for frame in inspect.stack()):
                raise OSError(failures.pop())
            return _original(*args, **kwargs)

        monkeypatch.setattr(owner, name, read)
    return failures


def test_watch_never_silent_follow_prints_unknown_on_stdout_and_keeps_watching(
    git_history, tmp_path, monkeypatch, capsys,
):
    from foragent import orch

    case = snapshot_case(git_history, tmp_path, monkeypatch)
    main = local_cli(case, monkeypatch)
    record_gh(monkeypatch)
    failures = fail_first_read(case, monkeypatch)
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) > 3:
            raise KeyboardInterrupt

    monkeypatch.setattr(orch.time, "sleep", sleep)
    assert main(["orch", "watch", "--interval", "1", "--follow"]) == 0
    captured = capsys.readouterr()
    lines = [json.loads(line) for line in captured.out.splitlines() if line.strip()]
    assert failures == [], "The failing read was never attempted"
    assert lines, "A failed read must reach stdout, where a Monitor listens"
    first = lines[0]
    assert first.get("unknown") is True and "disk busy" in first.get("error", "")
    assert "blocked" not in first and "needs_human" not in first and "merged" not in first, (
        "An unreadable state must not be reported as empty sets")
    later = [line for line in lines[1:] if "blocked" in line]
    assert later, "The watcher stopped after one failed read instead of retrying"
    assert later[0]["blocked"] == [2] and later[0]["needs_human"] == [3]
    assert sleeps and all(seconds <= 1 for seconds in sleeps)


def test_watch_never_silent_one_shot_watch_reports_unknown_and_fails(
    git_history, tmp_path, monkeypatch, capsys,
):
    case = snapshot_case(git_history, tmp_path, monkeypatch)
    main = local_cli(case, monkeypatch)
    record_gh(monkeypatch)
    fail_first_read(case, monkeypatch)
    before = (case.store.root / "orch-watch.json").read_text() if (
        case.store.root / "orch-watch.json").exists() else None
    assert main(["orch", "watch", "--interval", "1"]) != 0
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert len(lines) == 1 and lines[0].get("unknown") is True and "disk busy" in lines[0]["error"]
    after = (case.store.root / "orch-watch.json").read_text() if (
        case.store.root / "orch-watch.json").exists() else None
    assert after == before, "An unknown reading must not overwrite the last known snapshot"


def test_watch_never_silent_recovered_read_emits_the_real_snapshot_even_if_unchanged(
    git_history, tmp_path, monkeypatch, capsys,
):
    case = snapshot_case(git_history, tmp_path, monkeypatch)
    main = local_cli(case, monkeypatch)
    record_gh(monkeypatch)
    assert main(["orch", "watch", "--interval", "1"]) == 0
    known = json.loads(capsys.readouterr().out)
    fail_first_read(case, monkeypatch)
    assert main(["orch", "watch", "--interval", "1"]) != 0
    capsys.readouterr()
    from foragent import orch

    monkeypatch.setattr(orch.time, "sleep", lambda seconds: pytest.fail(
        "The first good read after an unknown one must print at once"))
    assert main(["orch", "watch", "--interval", "1"]) == 0
    assert json.loads(capsys.readouterr().out) == known
