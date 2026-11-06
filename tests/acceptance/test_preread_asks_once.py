"""Acceptance tests for preread-asks-once (order: prepare and preread snags, 2026-11-06).

Offline: the implementer and the test writer/reviewer are the shared public fakes of the
preread and lane-tests fixtures; no real agent, GitHub call, commit, push or signal is used.
"""

import json
from pathlib import Path
import re

from foragent import executors
from lane_tests_case import (CANDIDATE, STATEMENT, TEST_PATH, lane_case,
                             offline_publication, prepare)
from test_order import plan, publishing
from test_order_preread import preread, preread_case
from test_skills_autosync import cli_exit


# ---------------------------------------------------------------------------
# Helpers


def instructions(prompt):
    """The preread's own instructions: everything before the order text it quotes."""
    head, marker, _ = prompt.partition("\n# Order:")
    assert marker, "the preread prompt must still quote the order after its instructions"
    return head


def paragraphs(text):
    return [part for part in re.split(r"\n\s*\n", text) if part.strip()]


def sentences(text):
    flat = " ".join(text.split())
    return [part for part in re.split(r"(?<=[.!?])\s+", flat) if part]


def asks_every_unknown_in_one_reply(text):
    every = re.compile(r"(?i)\b(all|every|each)\b[^.]{0,80}\b(unknowns?|questions?)\b")
    once = re.compile(r"(?i)\b(one|single|a single|the same)\s+(reply|answer|response|round|pass)\b|\bat once\b"
                      r"|\bin one go\b")
    return any(every.search(part) and once.search(part) and _clean(part)
               for part in sentences(text))


_NEGATED = re.compile(r"(?i)\b(not|never|no|don't|avoid|skip|without)\b\W+(?:\w+\W+){0,1}?"
                      r"(touch|touches|check|compare|verify|confirm|match|list|ask|all|every|each|decide)\b")


def _clean(part):
    """A sentence that negates the instruction it is checked for does not count."""
    return not _NEGATED.search(part)


def checks_files_against_code(text):
    files = re.compile(r"\bFiles\b")
    code = re.compile(r"(?i)\b(code|modules?|source|sources|files? the change)\b")
    touch = re.compile(r"(?i)\b(touch|touches|change|changes|modify|modifies|edit|edits)\b")
    check = re.compile(r"(?i)\b(check|compare|verify|confirm|match)\w*\b")
    return any(files.search(part) and code.search(part) and touch.search(part) and check.search(part)
               and _clean(part) for part in sentences(text))


def files_choice_is_decide(text):
    named = [part for part in sentences(text) if "DECIDE" in part and re.search(r"\bFiles\b", part)]
    about = [part for part in named if re.search(r"(?i)\b(not|instead of|rather than)\b[^.]{0,40}\bquestions?\b", part)]
    # Its opposite next to it (such a choice is not a DECIDE line) fails the rule.
    opposite = re.compile(r"(?i)\bnot\s+(?:an?\s+)?DECIDE\b")
    return (bool(about) and not any(opposite.search(part) for part in named) and any(
        "DECIDE" in part and re.search(r"\bFiles\b", part) and re.search(r"\bWHAT\b", part)
        for part in paragraphs(text)))


def progress_state(case, capsys, *args):
    capsys.readouterr()
    code = cli_exit(case.main, ["order", "progress", *args, "--repo-root", str(case.root)])
    out = capsys.readouterr().out
    assert code == 0, out
    return json.loads(out)


def shown(case, capsys):
    """What fa order progress shows now (every step prints the whole local record)."""
    return progress_state(case, capsys, "ledger", "--add", "preread rounds probe")


_ROUND = re.compile(r"(?i)round|preread|count|times")


def _matches(name, key):
    return isinstance(name, str) and (name == key or re.search(r"[/:# ]" + re.escape(key) + r"$", name))


def rounds_for(state, key):
    """Every count the progress record keeps for issue ``key`` (by name or by a key field)."""
    found = []

    def walk(value):
        if isinstance(value, dict):
            for name, inner in value.items():
                if _matches(name, key):
                    if type(inner) is int:
                        found.append(inner)
                    elif isinstance(inner, dict):
                        found.extend(v for n, v in inner.items() if type(v) is int and _ROUND.search(str(n)))
            if any(_matches(value.get(field), key) for field in ("key", "issue")):
                found.extend(v for n, v in value.items() if type(v) is int and _ROUND.search(str(n)))
            for name, inner in value.items():
                if name != "ledger":
                    walk(inner)
        elif isinstance(value, list):
            for inner in value:
                walk(inner)

    walk(state)
    return found


# ---------------------------------------------------------------------------
# Statement 1: every unknown in one reply, and Files checked against the code


def test_preread_prompt_asks_for_every_unknown_in_one_reply_and_to_check_files(preread_case, capsys):
    case = preread_case
    preread(case, capsys, "NONE", "NONE")
    for call in case.worker.calls:
        text = instructions(call["prompt"])
        assert asks_every_unknown_in_one_reply(text), (
            "The preread must ask the implementer to list every unknown it can find in one reply:\n" + text)
        assert checks_files_against_code(text), (
            "The preread must ask the implementer to check the issue's Files against the code the change "
            "must touch:\n" + text)
        # The order text the implementer reads is still all there.
        assert "## Files" in call["prompt"] and "## Out of scope" in call["prompt"]


# ---------------------------------------------------------------------------
# Statement 2: a choice inside Files that does not change WHAT is a DECIDE line


def test_preread_prompt_makes_a_choice_inside_files_a_decide_line(preread_case, capsys):
    """The prompt rule, and the reply that follows it lands as a decision, not as an unknown."""
    case = preread_case
    decide = "keep the login handler in api/login.py and name the setting login_timeout"
    payload = preread(case, capsys, f"APPROACH: add the handler\nDECIDE: {decide}", "NONE")
    for call in case.worker.calls:
        text = instructions(call["prompt"])
        assert files_choice_is_decide(text), (
            "The preread must say that a choice inside the issue's Files that does not change WHAT is a "
            "DECIDE line, not a question:\n" + text)
    api = payload["issues"][0]
    assert api["key"] == "api" and api["unknowns"] == [], "A DECIDE line is not a question"
    assert api.get("decisions") == [decide], "The DECIDE line is recorded once, as written"


# ---------------------------------------------------------------------------
# Statement 3: preread rounds per issue are recorded and shown by fa order progress


def test_preread_rounds_per_issue_are_counted_and_shown_by_order_progress(preread_case, capsys):
    case = preread_case
    progress_state(case, capsys, "start", "--title", case.plan["title"])
    before = shown(case, capsys)
    assert set(rounds_for(before, "api")) <= {0} and set(rounds_for(before, "client")) <= {0}

    preread(case, capsys, "QUESTION: Which region is the login API for?", "NONE")
    after_one = shown(case, capsys)
    assert rounds_for(after_one, "api") and set(rounds_for(after_one, "api")) == {1}, after_one
    assert set(rounds_for(after_one, "client")) == {1}, after_one

    # The planner answers; the implementer reads the changed issue again: a second round.
    case.plan["issues"][0]["goal"] += " The login API serves the EU region."
    case.path.write_text(json.dumps(case.plan), encoding="utf-8")
    preread(case, capsys, "NONE", "NONE")
    after_two = shown(case, capsys)
    assert set(rounds_for(after_two, "api")) == {2}, after_two
    assert set(rounds_for(after_two, "client")) == {2}, after_two

    # A round that reads only one issue counts for that issue alone.
    only_api = {**case.plan, "issues": [{**case.plan["issues"][0], "blocked_by": []}]}
    case.plan = only_api
    case.path.write_text(json.dumps(only_api), encoding="utf-8")
    preread(case, capsys, "NONE")
    after_three = shown(case, capsys)
    assert set(rounds_for(after_three, "api")) == {3}, after_three
    assert set(rounds_for(after_three, "client")) == {2}, after_three
    # The record is the shared one the lane view reads, not this process's memory.
    from foragent.state import StateStore

    assert set(rounds_for(StateStore.for_repo("owner/repo").order_progress(), "api")) == {3}


# ---------------------------------------------------------------------------
# Statement 4: after a PM answer, preparation resumes from the written and reviewed tests


def _record_writers(monkeypatch):
    """Wrap the fixture's writer fake: note its prompt and the tests already in its tree at start."""
    seen = []
    inner = executors.CodexExecutor.run

    def run(self, **kw):
        tree = Path(kw["worktree"])
        start = tree / TEST_PATH
        seen.append({"prompt": kw["prompt"], "tree": tree,
                     "existing": start.read_text() if start.is_file() else None})
        return inner(self, **kw)

    monkeypatch.setattr(executors.CodexExecutor, "run", run)
    return seen


def test_answered_question_resumes_from_the_written_and_reviewed_tests(lane_case, capsys, monkeypatch):
    case = lane_case
    writers = _record_writers(monkeypatch)
    # First pass: tests are written and reviewed; the reviewer asks planning a question.
    case.verdicts = ["question"]
    row = prepare(case, capsys)
    assert row["status"] == "question" and row["questions"] == ["Which delivery is meant?"]
    assert [call["role"] for call in case.calls] == ["writer", "reviewer"]
    assert len(writers) == 1 and writers[0]["existing"] is None
    reviewed = case.calls[1]["candidate"]
    assert reviewed == CANDIDATE

    # The PM answers in the issue (as the order's goals record answers); nothing else changes.
    case.plan["issues"][0]["goal"] = STATEMENT + " Planning reply: the delivery is the e-mail notice."
    case.save()
    case.candidate = CANDIDATE.replace('"send"]', '"send", "--email"]')
    case.verdicts = ["pass"]
    row = prepare(case, capsys)
    assert len(writers) == 2, "The answer must reach a writer that applies it"
    second = writers[1]
    carried = second["existing"] == reviewed or reviewed in second["prompt"]
    assert carried, ("After the answer, the writer must start from the tests already written and reviewed, "
                     "not from an empty acceptance directory")
    first_lines = set(writers[0]["prompt"].splitlines())
    ignore = set(CANDIDATE.splitlines()) | set(case.plan["issues"][0]["goal"].splitlines())
    added = "\n".join(line for line in second["prompt"].splitlines()
                      if line not in first_lines and line not in ignore)
    assert re.search(r"(?i)\bonly\b", added) and re.search(r"(?i)\b(answer|answers|answered|question)\b", added), (
        "The resumed writer must be told to change only what the answer affects:\n" + added)
    assert "Which delivery is meant?" in second["prompt"], "The resumed writer must see which question was answered"
    assert row["status"] == "reviewed", row
    # One more pass, not a restart that writes the tests twice.
    assert [call["role"] for call in case.calls] == ["writer", "reviewer", "writer", "reviewer"]
    # Preparing again with nothing new reuses the reviewed result.
    before = len(case.calls)
    assert prepare(case, capsys)["status"] == "reviewed"
    assert len(case.calls) == before

