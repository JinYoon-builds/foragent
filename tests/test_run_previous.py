"""Retry default, stop point and the conflict rebuild rule (#416)."""

from runner_support import run_case  # noqa: F401

from pathlib import Path
from types import SimpleNamespace

from foragent.executors import ExecResult
from foragent.run_attempt import ConflictBudgetExhausted
from foragent.run_conflicts import _rebuild_reason
from foragent.run_previous import add_hint, consume_hint, pending_hint, record_stop_point, retry_marker, stop_path
from foragent.run_prompts import DEFAULT_RETRY_HINT, build_resume_prompt


def test_retry_marker_adds_default_before_hint_and_is_consumed_with_it(tmp_path):
    assert pending_hint(tmp_path, 1) == (False, None)
    marker = retry_marker(tmp_path, 1)
    marker.parent.mkdir()
    marker.write_text("retry\n")
    (tmp_path / "hints/1.txt").write_text("Keep the locale")
    assert pending_hint(tmp_path, 1) == (True, "Keep the locale")
    prompt = build_resume_prompt(1, None, "body", retry_default=True, orchestrator_hint="Keep the locale")
    assert prompt.index(DEFAULT_RETRY_HINT) < prompt.index("Keep the locale")
    consume_hint(tmp_path, 1, "Keep the locale")
    assert pending_hint(tmp_path, 1) == (False, None)


def test_producers_only_append_and_delivery_removes_only_what_the_prompt_held(tmp_path):
    from foragent.state import StateStore

    store = StateStore(tmp_path)
    add_hint(store, 1, "Use the selected locale")
    add_hint(store, 1, "  ")  # Blank advice adds nothing.
    add_hint(store, 1, "Preserve the receipt timezone")
    _, delivered = pending_hint(tmp_path, 1)
    assert delivered == "Use the selected locale\n\nPreserve the receipt timezone"
    add_hint(store, 1, "Keep the second receipt field")  # Accepted after the prompt was read.
    consume_hint(tmp_path, 1, delivered)
    assert pending_hint(tmp_path, 1) == (False, "Keep the second receipt field")
    consume_hint(tmp_path, 1, "Keep the second receipt field")
    assert not (tmp_path / "hints/1.txt").exists()


def test_stop_point_prefers_final_message_then_this_turns_log_tail(tmp_path):
    log = tmp_path / "issue-1.log"
    log.write_text("earlier runner output\n")
    offset = log.stat().st_size
    log.write_text(log.read_text() + "worked on parser\nstopped before tests\n")
    record_stop_point(tmp_path, 1, ExecResult(0, False, "", log), log, offset)
    assert stop_path(tmp_path, 1).read_text() == "worked on parser\nstopped before tests\n"
    record_stop_point(tmp_path, 1, ExecResult(0, False, "Final summary", log), log, offset)
    assert stop_path(tmp_path, 1).read_text() == "Final summary\n"


def _attempt():
    ctx = SimpleNamespace(config=SimpleNamespace(conflict_retries=2))
    return SimpleNamespace(ctx=ctx, target_branch="main")


def test_rebuild_reason_needs_more_than_half_of_own_files():
    own = ["a", "b", "c", "d"]
    assert _rebuild_reason(_attempt(), ConflictBudgetExhausted("x", ["a", "b"], own)) is None
    assert _rebuild_reason(_attempt(), ConflictBudgetExhausted("x", ["a"], [])) is None
    reason = _rebuild_reason(_attempt(), ConflictBudgetExhausted("x", ["a", "b", "c"], own))
    assert reason == "3/4 of the PR's own files conflict with main after 2 in-place resolution attempts"


def test_path_helper_keeps_runs_issue_directory_for_reviews(tmp_path):
    assert stop_path(tmp_path, 7) == Path(tmp_path, "runs", "issue-7-stop.txt")


def test_review_class_question_follows_repository_language_in_pr_body_rules():
    from foragent.run_prompts import review_scope_rules

    assert review_scope_rules("tests/acceptance/").endswith(
        "For each finding, say whether it is one instance of a broader defect class "
        "and where else to look in the change.")
    korean = review_scope_rules("tests/acceptance/", "ko")
    assert "결함 종류" in korean and "broader defect class" not in korean


def test_stop_point_is_recorded_when_a_usage_limit_ends_the_turn(run_case, monkeypatch):
    from foragent.runner import run_issue

    c = run_case
    stale = stop_path(c.store.root, 1)
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_text("an earlier attempt's stop\n")

    def limited(**kwargs):
        with kwargs["log_path"].open("a", encoding="utf-8") as log:
            log.write("Half-way through the parser when the limit hit.\n")
        return ExecResult(1, False, "", kwargs["log_path"], error_kind="limit")

    monkeypatch.setattr(c.executor, "run", limited)
    assert run_issue(1, c.ctx) == "limit-wait"
    text = stale.read_text()
    assert "Half-way through the parser" in text and "earlier attempt" not in text


def _git(root, *args, stdin=None):
    import subprocess
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True,
                          input=stdin).stdout


def _commit(ref, message, files, parent=None):
    stream = [f"commit {ref}\n", "committer Tests <t@example.com> 1792483200 +0000\n",
              f"data {len(message)}\n{message}\n"]
    if parent is not None:
        stream.append(f"from {parent}\n")
    stream += [f"M 100644 inline {name}\ndata {len(text)}\n{text}\n" for name, text in files.items()]
    return "".join(stream)


def test_rebuild_judges_conflicts_still_unresolved_after_partial_resolution(tmp_path):
    from foragent.git import GitRepo
    from foragent.run_conflicts import _unresolved_conflicts

    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "issue")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "Tests")
    names = ["a.txt", "b.txt", "c.txt", "d.txt"]
    _git(root, "fast-import", "--quiet", stdin="".join([
        _commit("refs/heads/base", "base", {name: "base\n" for name in names}),
        _commit("refs/heads/issue", "issue", {name: "issue\n" for name in names}, "refs/heads/base"),
        _commit("refs/heads/main", "target", {name: "target\n" for name in names[:3]}, "refs/heads/base"),
    ]) + "done\n")
    _git(root, "read-tree", "-u", "--reset", "issue")
    git = GitRepo(root)
    try:
        _git(root, "merge", "main")
    except Exception:
        pass
    conflicts = git.conflict_paths()
    assert conflicts == names[:3]
    # The implementer resolved two of the three conflicts in place but did not stage them.
    (root / "a.txt").write_text("target\nissue\n")
    (root / "b.txt").write_text("target\nissue\n")
    remaining = _unresolved_conflicts(git, conflicts)
    assert remaining == ["c.txt"]
    assert _rebuild_reason(_attempt(), ConflictBudgetExhausted("x", remaining, names)) is None
    assert _rebuild_reason(_attempt(), ConflictBudgetExhausted("x", conflicts, names)) is not None


def test_rerun_review_adds_the_class_question_to_an_older_pr_scope():
    from foragent.i18n import text
    from foragent.run_prompts import review_scope_rules
    from foragent.run_review import with_review_scope

    question = text("review_class_question", "en")
    rules = review_scope_rules("tests/acceptance/")
    older = "Closes #1\n\n## Review scope\n\n" + rules.removesuffix("\n" + question) + "\nThe acceptance tests are locked."
    refreshed = with_review_scope(older, "tests/acceptance/", "en")
    assert rules in refreshed and refreshed.count(question) == 1
    assert with_review_scope(refreshed, "tests/acceptance/", "en") == refreshed
    assert with_review_scope("Closes #1", "tests/acceptance/", "en") == "Closes #1\n\n## Review scope\n\n" + rules
    custom = "Closes #1\n\n## Review scope\n\nOnly the API.\n"
    assert question in with_review_scope(custom, "tests/acceptance/", "en")
    korean = with_review_scope(older, "tests/acceptance/", "ko")
    assert text("review_class_question", "ko") in korean
