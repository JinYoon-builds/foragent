"""Acceptance test for pm-session-replaceable. Locked by Foragent; do not edit.

A planning (PM) session can be replaced at any time: a large transcript makes the
statusline suggest a fresh session, and the fresh session's `fa brief` names the
planning work still in flight, so no handoff note is needed.
"""

from datetime import timedelta
from io import BytesIO
import json
import re
from types import SimpleNamespace

import pytest
import yaml

from test_skills_autosync import cli_exit, isolated_home

MB = 1024 * 1024
HANGUL = re.compile("[가-힣]")


# --- 1. The statusline suggests a fresh planning session when the transcript is large ---

@pytest.fixture
def planning_folder(tmp_path, monkeypatch, isolated_home):
    """A Foragent project folder where `fa statusline` runs as the planning session."""
    from foragent import statusline

    root = tmp_path / "project"
    root.mkdir()
    (root / ".git").mkdir()
    config_path = root / ".foragent.yml"

    def configure(language):
        config_path.write_text(yaml.safe_dump({"repo": "offline/pmfresh", "language": language,
                                               "skill_libraries": []}), encoding="utf-8")

    configure("en")
    monkeypatch.chdir(root)
    monkeypatch.delenv(statusline.NESTED_ENV, raising=False)
    monkeypatch.delenv("FORAGENT_ROLE", raising=False)
    monkeypatch.delenv("FORAGENT_SESSION_ID", raising=False)
    record = statusline.record_path()
    record.parent.mkdir(parents=True, exist_ok=True)
    # The statusline is installed with no previous command: its output is Foragent's own.
    record.write_text(json.dumps({"enabled": True, "previous_command": None}), encoding="utf-8")
    transcripts = tmp_path / "transcripts"
    transcripts.mkdir()

    def transcript(size):
        """A sparse transcript file of ``size`` bytes (no real disk use)."""
        path = transcripts / f"session-{size}.jsonl"
        with path.open("wb") as handle:
            handle.truncate(size)
        return path

    def run(path=None):
        from foragent import cli

        payload = {"session_id": "pm-session", "cwd": str(root),
                   "model": {"display_name": "Opus"}, "workspace": {"current_dir": str(root)}}
        if path is not None:
            payload["transcript_path"] = str(path)
        output = BytesIO()
        with monkeypatch.context() as streams:
            streams.setattr("sys.stdin", SimpleNamespace(buffer=BytesIO(json.dumps(payload).encode())))
            streams.setattr("sys.stdout", SimpleNamespace(buffer=output))
            assert cli_exit(cli.main, ["statusline"]) == 0
        return output.getvalue().decode("utf-8")

    return SimpleNamespace(root=root, configure=configure, transcript=transcript, run=run,
                           missing=transcripts / "gone.jsonl")


def added_line(baseline: str, output: str) -> str:
    """The one suggestion line ``output`` adds to ``baseline``; fails unless exactly one line was added."""
    before, after = baseline.splitlines(), output.splitlines()
    assert len(after) in (len(before), len(before) + 1), (
        f"A large transcript adds at most one line to the statusline:\n{baseline!r}\n->\n{output!r}")
    changed = [line for line in after if line not in before]
    assert len(changed) == 1, f"Exactly one statusline line carries the suggestion: {changed!r}"
    line = changed[0]
    for kept in before:  # The suggestion may share the last line; the existing text stays first.
        if line.startswith(kept) and kept:
            line = line[len(kept):]
    return line.strip()


@pytest.mark.parametrize("language", ["en", "ko"])
def test_large_planning_transcript_adds_one_short_fresh_session_line(planning_folder, language):
    case = planning_folder
    case.configure(language)
    baseline = case.run()
    # Small, missing or absent transcripts leave the statusline exactly as it was.
    assert case.run(case.transcript(8 * MB)) == baseline, "A small transcript must add nothing"
    assert case.run(case.missing) == baseline, "A transcript path that does not exist must add nothing"
    # 180 MB transcripts were seen that the desktop and mobile apps could not render;
    # a transcript well under that already gets the suggestion.
    for size in (64 * MB, 180 * MB):
        line = added_line(baseline, case.run(case.transcript(size)))
        assert line, f"A {size // MB} MB transcript must add a suggestion"
        assert len(line) <= 120, f"The suggestion must be one short line: {line!r}"
        assert line.count("/fa") == 1, (
            f"The suggestion names /fa once, the way a fresh session picks up the work: {line!r}")
        if language == "ko":
            assert HANGUL.search(line), f"The suggestion follows the project language: {line!r}"
        else:
            assert not HANGUL.search(line), f"The suggestion follows the project language: {line!r}"


@pytest.mark.parametrize("role", ["view", ""], ids=["lane", "empty-role"])
def test_lane_sessions_never_get_the_fresh_session_suggestion(planning_folder, monkeypatch, role):
    case = planning_folder
    large = case.transcript(180 * MB)
    planning = case.run(large)
    assert planning != case.run(), "Control: the planning session sees the suggestion"
    monkeypatch.setenv("FORAGENT_ROLE", role)
    assert case.run(large) == case.run(), "A session with FORAGENT_ROLE set never gets the suggestion"


# --- 2. `fa brief` names the planning work still in flight ---

def brief_case(git_history, tmp_path, monkeypatch):
    from test_fa_brief import briefing_case

    return briefing_case(git_history, tmp_path, monkeypatch, language="en")


def brief_outputs(main, case, capsys):
    """Plain and --json briefing output, as text (field names are the lane's choice)."""
    capsys.readouterr()
    assert main(["brief", "--repo-root", str(case.root)]) == 0
    plain = capsys.readouterr().out
    assert main(["brief", "--json", "--repo-root", str(case.root)]) == 0
    raw = capsys.readouterr().out
    json.loads(raw)
    return plain, raw


def request(main, capsys, source, words):
    capsys.readouterr()
    assert main(["orch", "request", "--from", source, "--text", words]) == 0
    return re.search(r"Request (\S+) saved", capsys.readouterr().out)[1]


def test_brief_lists_open_requests_the_planning_session_sent(git_history, tmp_path, monkeypatch, capsys):
    case, main = brief_case(git_history, tmp_path, monkeypatch)
    waiting = "Split the watch footer pmfresh-waiting"
    taken = "Rerun the stalled lane pmfresh-taken"
    finished = "Retitle issue 4 pmfresh-finished"
    from_gate = "Tick failed twice pmfresh-gate"
    request(main, capsys, "planning", waiting)
    taken_id = request(main, capsys, "planning", taken)
    finished_id = request(main, capsys, "planning", finished)
    request(main, capsys, "gate", from_gate)
    assert main(["orch", "request-status", taken_id, "processing"]) == 0
    assert main(["orch", "request-status", finished_id, "done", "--answer", "Retitled #4"]) == 0
    plain, raw = brief_outputs(main, case, capsys)
    for output in (plain, raw):
        assert output.count(waiting) == 1, "An open planning request appears once in the briefing"
        assert output.count(taken) == 1, "A planning request the orchestrator is processing appears once"
        assert finished not in output, "A finished planning request is no longer in flight"
        assert from_gate not in output, "Only requests the planning session sent are its in-flight work"


def test_brief_lists_planning_branches_with_open_prs(git_history, tmp_path, monkeypatch, capsys):
    from conftest import git_command
    from foragent.scheduler import tick
    from test_orch_review_handoff import tick_context

    case, main = brief_case(git_history, tmp_path, monkeypatch)
    clone, origin = git_history.clone, git_history.origin
    main_tip = git_command(clone, "rev-parse", "origin/main")
    tree = git_command(clone, "rev-parse", "origin/main^{tree}")

    def branch(name, message):
        """A branch with one commit beyond main: locally, on origin and as a remote-tracking ref."""
        tip = git_command(clone, "commit-tree", tree, "-p", main_tip, "-m", message)
        git_command(clone, "update-ref", f"refs/heads/{name}", tip)
        git_command(clone, "update-ref", f"refs/remotes/origin/{name}", tip)
        git_command(origin, "fetch", "--quiet", str(clone), f"+refs/heads/{name}:refs/heads/{name}")
        return tip

    def pr(name, title):
        return case.tracker.create_pr(head=name, base="main", title=title, body="")

    branch("pm/order-pmfresh-live", "PM spec draft")
    pr("pm/order-pmfresh-live", "Order specs")
    # Squash-merged: its commit is not in main's history, but its PR is merged.
    squashed_tip = branch("pm/order-pmfresh-squashed", "Merged spec")
    squashed = pr("pm/order-pmfresh-squashed", "Merged specs")
    case.tracker.set_pr_head_sha(squashed, squashed_tip)
    case.tracker.merge_pr(squashed, expected_head_sha=squashed_tip, delete_branch=False)
    branch("pm/order-pmfresh-closed", "Dropped spec")
    case.tracker.close_pr(pr("pm/order-pmfresh-closed", "Dropped specs"), delete_branch=False)
    branch("side/pmfresh-other", "Someone else")
    pr("side/pmfresh-other", "Side work")
    # The gate observes GitHub on its next periodic tick; the briefing reads what it recorded.
    case.clock[0] += timedelta(minutes=10)
    tick(tick_context(case, start_runs=False))
    plain, raw = brief_outputs(main, case, capsys)
    for output in (plain, raw):
        assert output.count("pm/order-pmfresh-live") == 1, (
            "A planning branch with an open PR appears once in the briefing")
        assert "pm/order-pmfresh-squashed" not in output, "A planning branch whose PR merged is not in flight"
        assert "pm/order-pmfresh-closed" not in output, "A planning branch whose PR was closed is not in flight"
        assert "side/pmfresh-other" not in output, "Only pm/* branches are the planning session's work"
