"""Acceptance test for watch-reexec. Locked by Foragent; do not edit."""

import os
import sys
from concurrent.futures import Future

import pytest

import test_view_reload
from test_skills_autosync import isolated_home
from test_view_reload import installed_build, live_view


def view_session(git_history, directory, monkeypatch, metadata):
    """Reuse the bounded Screen, Worker, clock, CLI and execv fakes from view-reload."""
    directory.mkdir()
    with monkeypatch.context() as patch:
        return live_view(git_history, directory, patch, metadata)


def test_replaced_build_reexecs_during_pending_refresh_and_new_process_keeps_same_sha(
    git_history, tmp_path, monkeypatch, installed_build,
):
    from foragent import cli, watch

    # Seam: watch._installed_build() must freshly read installed _build.py,
    # rather than reuse the GIT_SHA cached in sys.modules (or the CLI).
    source = tmp_path / "_build.py"
    old_sha, new_sha = installed_build.sha, "d4e5f6a"
    source.write_text(f"GIT_SHA = '{old_sha}'\n")
    timestamp = source.stat().st_mtime_ns
    build = sys.modules["foragent._build"]
    monkeypatch.setattr(build, "__file__", str(source), raising=False)
    monkeypatch.setattr(build, "GIT_SHA", old_sha, raising=False)
    original_frame = watch._frame
    replacements = []
    pending = []

    class PendingRefresh(Future):
        def set_result(self, result):
            # Extend the existing Worker fake with a refresh that stays pending.
            # Publishing its result must not be required to notice new code.
            pending.append(self)

    monkeypatch.setattr(test_view_reload, "Future", PendingRefresh)

    def frame(*args, **kwargs):
        result = original_frame(*args, **kwargs)
        if pending and not replacements:
            # Replace installed metadata while a snapshot refresh is in flight.
            source.write_text(f"GIT_SHA = '{new_sha}'\n")
            os.utime(source, ns=(timestamp, timestamp))
            replacements.append(installed_build.frame())
        return result

    monkeypatch.setattr(watch, "_frame", frame)
    first, argv = view_session(git_history, tmp_path / "old-process", monkeypatch, installed_build)
    assert build.GIT_SHA == old_sha, "Exercise a stale imported build, not a mutated constant"
    assert replacements and pending and not pending[0].done(), "Exercise replacement during an unfinished refresh"
    assert first.exec_calls == [(argv[0], argv)], (
        "A changed installed build must re-exec with the original entry point and full argv "
        "between frames, even while a snapshot refresh remains pending"
    )
    assert first.events[-2:] == ["restored", "execv"], "Unwind curses before execv"

    # A successful exec cannot return. Model the new process with another CLI
    # session: it starts on the new SHA and must not re-exec that SHA again.
    monkeypatch.setattr(build, "GIT_SHA", new_sha)
    monkeypatch.setattr(cli, "GIT_SHA", new_sha)
    installed_build.sha = new_sha
    second, _ = view_session(git_history, tmp_path / "new-process", monkeypatch, installed_build)
    assert second.exec_calls == [], "The new process must accept its own build without a restart loop"
    assert second.frames == 80, "The unchanged build must keep drawing until the fake user quits"


@pytest.mark.parametrize("error_type", [ImportError, ModuleNotFoundError])
def test_frame_import_failure_reexecs_but_other_frame_errors_still_propagate(
    git_history, tmp_path, monkeypatch, installed_build, error_type,
):
    from foragent import watch

    stable, _ = view_session(git_history, tmp_path / "stable", monkeypatch, installed_build)
    assert stable.exec_calls == [], "An unchanged installed build must not re-exec"
    original_frame = watch._frame

    def fail_one_frame(error):
        failures = [error]

        def frame(*args, **kwargs):
            if failures and installed_build.frame() == 1:
                raise failures.pop()
            return original_frame(*args, **kwargs)

        return frame

    ordinary = RuntimeError("drawing failed for a reason unrelated to imports")
    monkeypatch.setattr(watch, "_frame", fail_one_frame(ordinary))
    with pytest.raises(RuntimeError) as observed:
        view_session(git_history, tmp_path / "ordinary-error", monkeypatch, installed_build)
    assert observed.value is ordinary, "Keep today's propagation of non-import frame errors"

    missing = error_type("package temporarily missing during reinstall")
    monkeypatch.setattr(watch, "_frame", fail_one_frame(missing))
    escaped = None
    result = None
    try:
        result = view_session(git_history, tmp_path / "partial-install", monkeypatch, installed_build)
    except ImportError as exc:
        escaped = exc

    assert escaped is None, f"A frame {error_type.__name__} must re-exec instead of escaping: {escaped}"
    state, argv = result
    assert state.exec_calls == [(argv[0], argv)], "A partial install must replace the view with the same argv"
    assert state.events[-2:] == ["restored", "execv"], "Restore the terminal before replacing the process"
    assert state.frames == 1, "Recover from the import failure without drawing another stale frame"
