"""Standalone options and error paths beyond the fixed branch review acceptance."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from foragent import review_branch, run_review
from foragent.commands import review as command
from foragent.config import Config
from foragent.run_review import ReviewResult
from foragent.state import StateStore
from foragent.tracker import GhTracker


def test_goal_is_verbatim_and_does_not_require_an_issue(tmp_path, monkeypatch):
    def forbidden(*args):
        pytest.fail("A direct Goal must not read issue state")

    monkeypatch.setattr(StateStore, "for_repo", forbidden)
    for goal in (None, "Keep uploads bounded.\nRetain diagnostics."):
        parsed = review_branch.goal_for(tmp_path, Config(), goal, None)
        assert parsed.goal == (goal or "")
        assert parsed.out_of_scope == ""


@pytest.mark.parametrize("cached", [True, False])
def test_issue_grounding_reads_cache_before_github(tmp_path, monkeypatch, cached):
    """--issue reads only the local issue cache; a cache miss stops plainly and never calls GitHub."""
    body = "## Goal\nBound retries.\n\n## Out of scope\nTransport changes.\n\n## Done when\n```sh\nfalse\n```\n"
    rows = [{"number": 42, "body": body}] if cached else [{"number": 7, "body": body}]
    monkeypatch.setattr(StateStore, "local_issues", lambda self: rows)
    calls = []

    def get_issue(self, number):
        calls.append((self.repo, number))
        return SimpleNamespace(body=body)

    monkeypatch.setattr(GhTracker, "get_issue", get_issue)
    if cached:
        parsed = review_branch.goal_for(tmp_path, Config(repo="owner/repo"), None, 42)
        assert parsed.goal == "Bound retries."
        assert parsed.out_of_scope == "Transport changes."
        assert parsed.done_when_commands == []
    else:
        with pytest.raises(review_branch.ReviewError, match="#42"):
            review_branch.goal_for(tmp_path, Config(repo="owner/repo"), None, 42)
    assert calls == []
    assert not any(tmp_path.iterdir())


def test_standalone_finish_scores_once_without_run_journal(monkeypatch):
    calls = []
    original = ReviewResult("FAIL", "src/upload.py:3: retry loops")
    scored = ReviewResult("PASS", "", review_notes=("not grounded",))

    def score(n, ctx, review, round_number, **kwargs):
        calls.append((n, review.findings, round_number, kwargs))
        return scored

    def forbidden(*args, **kwargs):
        pytest.fail("Standalone finish must not journal or update run state")

    monkeypatch.setattr(run_review.review_scoring, "score", score)
    monkeypatch.setattr(run_review, "_journal_review", forbidden)
    monkeypatch.setattr(run_review, "_record_notes", forbidden)
    result = run_review._finish_review(0, object(), original, 1, ("VERDICT: FAIL",),
                                       git="git", parsed="parsed", journal=False)
    assert result is scored
    assert calls == [(0, original.findings, 1, {"git": "git", "parsed": "parsed", "record": False})]


@pytest.mark.parametrize("result,code", [
    (ReviewResult("UNUSABLE", "", notes=("missing verdict",)), 2),
    (ReviewResult("PASS", "", conflicts=("SPEC-CONFLICT: unresolved Goal",)), 2),
    (ReviewResult("FAIL", "finding", execution_failed=True), 2),
])
def test_errors_are_not_pass_or_fail(monkeypatch, capsys, result, code):
    monkeypatch.setattr(review_branch, "review", lambda *args, **kwargs: (result, "en", False))
    args = SimpleNamespace(target="feature", base="main", goal=None, issue=None)
    assert command.run(args) == code
    out = capsys.readouterr().out
    assert f"VERDICT: {result.verdict}" in out


@pytest.mark.parametrize("failed_cleanup", ["none", "directory"])
def test_cleanup_failures_preserve_the_original_review_error(tmp_path, monkeypatch, failed_cleanup):
    original = ValueError("panel original error")
    real_temporary = review_branch.TemporaryDirectory
    paths = []

    class Directory:
        def __init__(self, **kwargs):
            self.inner = real_temporary(**kwargs)
            self.name = self.inner.name
            paths.append(self.inner.name)

        def cleanup(self):
            self.inner.cleanup()
            if failed_cleanup == "directory":
                raise OSError("directory cleanup failed")

    class Git:
        def __init__(self, root):
            self.root = root

        def _call(self, args):
            if args == ["rev-parse", "--show-toplevel"]:
                return str(tmp_path)
            return "commit"

    def fail(*args, **kwargs):
        raise original

    monkeypatch.setattr(review_branch, "GitRepo", Git)
    monkeypatch.setattr(review_branch, "TemporaryDirectory", Directory)
    monkeypatch.setattr(review_branch, "resolve_target", lambda *args: ("head", False))
    monkeypatch.setattr(review_branch, "load_config", lambda *args: Config())
    monkeypatch.setattr(review_branch, "_panel_review", fail)
    with pytest.raises(ValueError) as caught:
        review_branch.review("feature")
    assert caught.value is original
    assert len(paths) == 1
    assert not Path(paths[0]).exists(), "the scratch checkout goes with its temporary directory"
    notes = " ".join(getattr(original, "__notes__", ()))
    assert ("directory cleanup failed" in notes) == (failed_cleanup == "directory")


def test_a_cleanup_failure_after_success_is_still_an_error():
    calls = []

    def fail():
        calls.append("cleanup")
        raise OSError("cleanup only")

    with pytest.raises(OSError, match="cleanup only"):
        with review_branch._cleanup(fail):
            pass
    assert calls == ["cleanup"]


def test_cli_prints_original_error_and_cleanup_diagnostic(monkeypatch, capsys):
    original = ValueError("panel original error")
    original.add_note("Cleanup also failed: worktree cleanup failed")

    def fail(*args, **kwargs):
        raise original

    monkeypatch.setattr(review_branch, "review", fail)
    args = SimpleNamespace(target="feature", base="main", goal=None, issue=None)
    assert command.run(args) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "panel original error" in captured.err
    assert "worktree cleanup failed" in captured.err
    assert len(captured.err.splitlines()) == 1


@pytest.mark.parametrize("target_kind", ["branch", "worktree"])
def test_review_runs_no_program_any_caller_setup_configures(tmp_path, monkeypatch, target_kind):
    """No hook, filter or fsmonitor from any config source the caller has runs during `fa review`."""
    import os
    import subprocess

    repo = tmp_path / "repo"
    git = lambda *args, cwd=repo: subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout
    repo.mkdir()
    git("init", "-q", "-b", "main")
    env = {"GIT_AUTHOR_NAME": "a", "GIT_AUTHOR_EMAIL": "a@b", "GIT_COMMITTER_NAME": "a", "GIT_COMMITTER_EMAIL": "a@b"}

    def commit(ref, message, parent=None):
        # Plumbing commits: the shared test guard forbids `git commit`.
        git("add", "-A")
        tree = git("write-tree").strip()
        sha = subprocess.run(["git", "commit-tree", tree, "-m", message, *(["-p", parent] if parent else [])], cwd=repo,
                             check=True, capture_output=True, text=True, env={**os.environ, **env}).stdout.strip()
        git("update-ref", ref, sha)
        return sha

    (repo / ".gitattributes").write_text("*.txt filter=mark\n")
    (repo / "a.txt").write_text("base\n")
    base = commit("refs/heads/main", "base")
    (repo / "a.txt").write_text("feature\n")
    commit("refs/heads/feature", "feature", base)
    (repo / "a.txt").write_text("base\n")
    git("read-tree", "main")
    target = "feature"
    if target_kind == "worktree":
        target = str(tmp_path / "feature-tree")
        git("worktree", "add", "-q", target, "feature")
    marks = tmp_path / "marks"  # each source writes its own marker file here
    marks.mkdir()

    def program(label, filter_command=False):
        script = tmp_path / "programs" / label
        script.parent.mkdir(exist_ok=True)
        script.write_text(f"#!/bin/sh\ntouch '{marks}/{label}'\n" + ("cat\n" if filter_command else ""))
        script.chmod(0o755)
        return str(script)

    def hooks(label):
        directory = tmp_path / f"hooks-{label}"
        directory.mkdir()
        for hook in ("post-checkout", "reference-transaction", "post-index-change", "pre-commit"):
            (directory / hook).write_text(f"#!/bin/sh\ntouch '{marks}/{label}-{hook}'\n")
            (directory / hook).chmod(0o755)
        return str(directory)

    def driver(label):
        return {f"filter.{label}.smudge": program(f"{label}-smudge", True),
                f"filter.{label}.clean": program(f"{label}-clean", True), f"filter.{label}.required": "true"}

    # 1. The repository's own config, hooks directory and tracked .gitattributes (filter=mark).
    for hook in ("post-checkout", "reference-transaction", "post-index-change", "pre-commit"):
        (repo / ".git" / "hooks" / hook).write_text(f"#!/bin/sh\ntouch '{marks}/repo-{hook}'\n")
        (repo / ".git" / "hooks" / hook).chmod(0o755)
    for key, value in {**driver("mark"), "core.fsmonitor": program("repo-fsmonitor")}.items():
        git("config", key, value)
    # 2. .git/info/attributes naming a filter the repository config defines.
    (repo / ".git" / "info").mkdir(exist_ok=True)
    (repo / ".git" / "info" / "attributes").write_text("* filter=info\n")
    for key, value in driver("info").items():
        git("config", key, value)
    # 3. User (global) and system config files.
    for scope, name in (("global", "GIT_CONFIG_GLOBAL"), ("system", "GIT_CONFIG_SYSTEM")):
        settings = {"core.hooksPath": hooks(scope), "core.fsmonitor": program(f"{scope}-fsmonitor"),
                    "core.attributesFile": str(tmp_path / f"{scope}-attributes"), **driver(scope)}
        (tmp_path / f"{scope}-attributes").write_text(f"* filter={scope}\n")
        lines = "".join(f"[{key.rsplit('.', 1)[0].replace('.', ' \"', 1) + ('\"' if key.count('.') > 1 else '')}]\n"
                        f"\t{key.rsplit('.', 1)[1]} = {value}\n" for key, value in settings.items())
        (tmp_path / f"{scope}.gitconfig").write_text(lines)
        monkeypatch.setenv(name, str(tmp_path / f"{scope}.gitconfig"))
    monkeypatch.delenv("GIT_CONFIG_NOSYSTEM", raising=False)
    # 4. Inherited command-line style overrides: GIT_CONFIG_PARAMETERS and GIT_CONFIG_COUNT.
    parameters = {"core.hooksPath": hooks("parameters"), "core.fsmonitor": program("parameters-fsmonitor"),
                  **driver("mark")}
    monkeypatch.setenv("GIT_CONFIG_PARAMETERS", " ".join(f"'{key}={value}'" for key, value in parameters.items()))
    counted = {"core.hooksPath": hooks("count"), "core.fsmonitor": program("count-fsmonitor")}
    monkeypatch.setenv("GIT_CONFIG_COUNT", str(len(counted)))
    for index, (key, value) in enumerate(counted.items()):
        monkeypatch.setenv(f"GIT_CONFIG_KEY_{index}", key)
        monkeypatch.setenv(f"GIT_CONFIG_VALUE_{index}", value)
    probe = subprocess.run(["git", "config", "--get", "core.hooksPath"], cwd=repo, capture_output=True, text=True)
    assert probe.stdout.strip().endswith("hooks-parameters"), "the overrides are live for an ordinary Git call"
    before = dict(os.environ)
    seen = {}

    def panel(n, ctx, checkout, *args, **kwargs):
        state = checkout.review_state()
        checkout._call(["update-ref", "HEAD", checkout.head_sha()])
        (checkout.root / "a.txt").write_text("edited by a reviewer\n")
        checkout._call(["add", "-A"])
        checkout._call(["status", "--porcelain"])
        checkout.restore_review_state(state)
        seen["file"] = (checkout.root / "a.txt").read_text()
        return ReviewResult("PASS", "")

    monkeypatch.chdir(repo)
    monkeypatch.setattr(review_branch, "_panel_review", panel)
    monkeypatch.setattr(review_branch, "load_config", lambda root: Config())
    result, _language, _worktree = review_branch.review(target)
    assert result.verdict == "PASS"
    assert seen["file"] == "feature\n", "the checkout holds the branch's blob, unfiltered"
    assert sorted(path.name for path in marks.iterdir()) == [], "caller-configured programs ran"
    assert dict(os.environ) == before


# --- #2215: a signal stops the review promptly and is never a verdict -----------------------
# No OS signal is sent: the installed SIGINT handler runs on the main thread through
# _thread.interrupt_main, only while review() has its own handler installed. Reviewers are
# in-process fakes, so no process is signalled.

def _acceptance_helpers():
    import importlib
    import sys

    sys.path.insert(0, str(Path(__file__).parent / "acceptance"))
    return importlib.import_module("test_review_branch")


def _signal_self_once(sent):
    """Run the installed SIGINT handler on the main thread, as a delivered Ctrl-C would.

    No OS signal is sent: _thread.interrupt_main only simulates its arrival in this process.
    """
    import _thread
    import signal

    if not sent:
        sent.append(True)
        _thread.interrupt_main(signal.SIGINT)


def _review_threads():
    import threading

    return [thread for thread in threading.enumerate() if thread.name.startswith("review-")]


def _finished_within(threads, seconds):
    import time

    deadline = time.monotonic() + seconds
    for thread in threads:
        thread.join(max(0.0, deadline - time.monotonic()))
    return [thread.name for thread in threads if thread.is_alive()]


def test_a_signal_ends_reviewers_waiting_for_machine_admission(tmp_path, monkeypatch, capsys):
    from foragent import capacity

    helpers = _acceptance_helpers()
    helpers.build(tmp_path, monkeypatch)
    helpers.use(monkeypatch, helpers.Script(review="VERDICT: PASS"))
    sent, waiting = [], []

    def overloaded():
        # Every admission sees an overloaded machine; once a reviewer is waiting, Ctrl-C.
        waiting.append(True)
        if len(waiting) >= 3:
            _signal_self_once(sent)
        return "overload"

    monkeypatch.setattr(capacity, "_sensor_block", overloaded)
    before = set(_review_threads())

    code, out, err = helpers.fa(["review", "feature", "--base", "main"], capsys)

    assert sent and code == 130, f"{code}\n{out}\n{err}"
    assert "VERDICT" not in out + err and "Traceback" not in out + err
    alive = _finished_within([thread for thread in _review_threads() if thread not in before], 3)
    assert alive == [], f"Reviewers still waiting for admission after the stop: {alive}"


def test_a_signal_ends_a_reviewer_in_retry_backoff(tmp_path, monkeypatch, capsys):
    import contextvars
    from concurrent.futures import ThreadPoolExecutor
    import threading

    helpers = _acceptance_helpers()
    helpers.build(tmp_path, monkeypatch)
    sleeping, outcome = threading.Event(), []

    def backoff(ctx):
        sleeping.set()
        try:
            ctx.sleep(600)  # the executor retry backoff (run_waits._executor_wait) uses ctx.sleep
        except BaseException as error:
            outcome.append(type(error).__name__)
            raise
        outcome.append("slept")

    def panel(n, ctx, *args, **kwargs):
        pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="review-backoff")
        try:  # as run_review._panel_review shuts its pool down
            future = pool.submit(contextvars.copy_context().run, backoff, ctx)
            sleeping.wait(5)
            _signal_self_once([])
            pool.shutdown(wait=True)
        except BaseException:
            pool.shutdown(wait=False, cancel_futures=True)
            raise
        return future.result()

    monkeypatch.setattr(review_branch, "_panel_review", panel)

    code, out, err = helpers.fa(["review", "feature", "--base", "main"], capsys)

    assert code == 130 and "Traceback" not in out + err, f"{code}\n{out}\n{err}"
    alive = _finished_within(_review_threads(), 3)
    assert alive == [] and outcome == ["Cancelled"], f"backoff did not end: {alive} {outcome}"


def test_a_signal_during_the_findings_merge_is_a_stop_not_a_verdict(tmp_path, monkeypatch, capsys):
    helpers = _acceptance_helpers()
    helpers.build(tmp_path, monkeypatch)
    from foragent.run_prompts import PANEL_MERGE_PROMPT

    sent = []

    class Merging(helpers.Script):
        def run(self, **kwargs):
            result = super().run(**kwargs)
            if PANEL_MERGE_PROMPT in kwargs["prompt"]:  # only the findings merge carries it
                _signal_self_once(sent)
            return result

    helpers.use(monkeypatch, Merging(review="VERDICT: PASS"))

    code, out, err = helpers.fa(["review", "feature", "--base", "main"], capsys)

    assert sent, f"the merger ran\n{code}\n{out}\n{err}"
    assert code == 130, f"A stop during the merge is not UNUSABLE (exit 2)\n{out}\n{err}"
    assert "VERDICT" not in out and "NOTE" not in out and "Traceback" not in out + err
    assert "stopped" in err.lower()
