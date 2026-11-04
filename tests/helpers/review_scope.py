"""Shared offline helpers for unit tests."""

import json

from foragent.executors import ExecResult

from helpers.review_case import make_case


OLD_FINDING = "feature.txt:1 — the previously accepted path loses a receipt"
NEW_FINDING = "feature.txt:2 — the changed retry path duplicates a receipt"
FIRST_FINDING = "feature.txt:2 — the first implementation needs repair"
PASS = "VERDICT: PASS\nNOTE: PASS-RECEIPT-47: reviewed the issue's receipt paths"


def content(accepted="reviewed", retry=1, base=0):
    # Far-apart lines let real Git separate an issue edit and an upstream edit
    # in the SAME file. Path-only filtering cannot satisfy this lock.
    return (f"accepted = {accepted!r}\nretry = {retry}\n"
            + "".join(f"padding_{i} = {i}\n" for i in range(21))
            + f"base = {base}\n")


def case_for(git_history, tmp_path, monkeypatch, *, reviewer="codex", **settings):
    git_history.update({"feature.txt": content("initial", 0)})
    return make_case(
        git_history, tmp_path, monkeypatch, reviewer=reviewer,
        executor="claude" if reviewer == "codex" else "codex",
        **{"skill_libraries": [], "test_command": "true", "language": "en",
           "max_retries": 1, "review_max": 0, **settings},
    )


def implement(text):
    def run(call):
        (call["worktree"] / "feature.txt").write_text(text)
        return "Implemented"
    return run


def capture_review(case, replies, *, calls=None):
    contexts = []
    replies = iter(replies)

    def run(**call):
        if calls is not None:
            calls.append(call)
        local = call["worktree"] / "CLAUDE.local.md"
        context = call["prompt"] + (local.read_text() if local.is_file() else "")
        if call.get("review_pr") is not None:
            context += case.tracker.get_issue(call["review_pr"]).body
        contexts.append(context)
        reply = next(replies)
        text = reply(call) if callable(reply) else reply
        if call.get("review_level") is not None:
            text = json.dumps({"result": text})
        return ExecResult(0, False, text, call["log_path"])

    case.reviewer.run = run
    return contexts
