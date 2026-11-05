"""Shared offline helpers for unit tests."""

import json
import os
import shutil
import signal

import pytest
import yaml

from conftest import GitHistory, git_command
from foragent.executors import ExecResult

from helpers.home import isolated_home
from helpers.review_case import make_case


_RISK_FILE = ".foragent-risks.yml"
_TIERS = {
    "s": {"codex_effort": "low", "claude_model": "haiku"},
    "m": {"codex_effort": "medium", "claude_model": "sonnet"},
    "l": {"codex_effort": "max", "claude_model": "opus"},
}


def entry(paths=None, **overrides):
    return {
        "paths": paths or ["feature*.txt"],
        "why": "RISK-MATCH: process ownership was trusted without evidence.",
        "incident": {"date": "2026-10-24", "summary": "INCIDENT-MATCH: a fake identity reached the signal sender."},
        "guidance": "GUIDANCE-MATCH: read only; use fake process tables and inspect weakened safety lines.",
        "added": {"by": "orchestrator", "at": "2026-10-24T12:00:00+00:00", "event": "incident:fixture"},
        **overrides,
    }


@pytest.fixture(autouse=True)
def offline(monkeypatch, isolated_home):
    from foragent import cli, executors

    def forbidden(*args, **kwargs):
        pytest.fail("Risk-map acceptance must not send signals, contact GitHub, or run a model")

    monkeypatch.setattr(os, "kill", forbidden)
    monkeypatch.setattr(os, "killpg", forbidden)
    monkeypatch.setattr(signal, "raise_signal", forbidden)
    monkeypatch.setattr(signal, "pthread_kill", forbidden)
    monkeypatch.setattr(executors, "run_logged", forbidden)
    monkeypatch.setattr(cli, "GhTracker", forbidden)


def risk_case(git_history, tmp_path, monkeypatch, *, entries=None, reviewer="codex", size="s",
              changed_paths=("feature.txt",), **settings):
    if entries is not None:
        text = yaml.safe_dump(entries, sort_keys=False)
        git_history.update({_RISK_FILE: text})
        # Both the project root and the runner's separate worktree see the map.
        (git_history.clone / _RISK_FILE).write_text(text, encoding="utf-8")
    case = make_case(
        git_history, tmp_path, monkeypatch,
        **{"executor": "claude" if reviewer == "codex" else "codex", "reviewer": reviewer,
           "reviewer_model": "ordinary-review-model", "tiers": _TIERS, "skill_libraries": [], **settings},
    )
    case.tracker.add_labels(1, [f"size:{size}"])
    # The ordinary Done-when fixture always creates feature.txt as well.
    changed_paths = tuple(dict.fromkeys(("feature.txt", *changed_paths)))
    case.tracker.set_pr_files(2, list(changed_paths))

    def implement(kwargs):
        case.implement(kwargs)
        for name in changed_paths:
            path = kwargs["worktree"] / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("changed\n", encoding="utf-8")

    case.review_contexts = []
    case.review_diffs = []

    def review(kwargs):
        case.review_diffs.append(case.git.at(kwargs["worktree"]).staged_diff("origin/main"))
        context = kwargs["prompt"]
        if reviewer == "claude":
            # Native /code-review reads the local scope or the PR body rather
            # than stdin. Observe these at the fake executor boundary, before
            # temporary scope cleanup, as the existing scope tests do.
            local = kwargs["worktree"] / "CLAUDE.local.md"
            context += local.read_text(encoding="utf-8") if local.is_file() else ""
            context += "\n".join(pr.body for pr in case.tracker.created_prs)
        case.review_contexts.append(context)
        # Match the public executor's native-vs-prompt transport option.
        return json.dumps({"result": "(none)"}) if reviewer == "claude" and kwargs.get("review_level") is not None else "VERDICT: PASS"

    case.implementer.steps = [implement]
    # Expect one response per perspective and one merged response. The fake
    # transport repeats its unchanged passing judgment; it never schedules work.
    def review_call(**kwargs):
        case.reviewer.calls.append(kwargs)
        return ExecResult(0, False, review(kwargs), kwargs["log_path"])

    case.reviewer.run = review_call
    return case


def copy_history(history, destination):
    destination.mkdir()
    origin = shutil.copytree(history.origin, destination / "origin.git")
    clone = shutil.copytree(history.clone, destination / "clone")
    git_command(clone, "config", "remote.origin.url", str(origin))
    return GitHistory(origin, clone, history.tip)
