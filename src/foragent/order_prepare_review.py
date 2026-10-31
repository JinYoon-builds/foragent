"""The independent review of a lane's written tests and their promotion into the plan (#1048).

`fa order prepare` stays the only writer of the preparation record and the only promoter of
reviewed bytes into the plan. The reviewer, a new read-only session of the configured reviewer
executor, returns a verdict with its evidence; this module only records it, checks the facts the
verdict rests on (every command reported failing on current main, every statement mapped,
unchanged candidate bytes) and never judges what a test means. Foragent itself runs no candidate
code: the reviewer runs the commands inside the same sandbox as the writer (the scratch tree is
its only writable place, no network, no hooks), and Claude gets no edit tools at all.

Review attempts are appended to the issue's ``attempts`` next to the writer's::

    {"attempt", "role": "reviewer", "cycle", "started_at", "finished_at", "status",
     "base_revision", "scratch", "executor", "model", "log", "exit_code", "timed_out",
     "session_id", "verdict", "coverage", "red", "questions", "reason"}
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from foragent import order_prepare_setup as setup, order_scratch
from foragent.config import Config, resolved_reviewer
from foragent.executors import executor_for
from foragent.git import GitError
from foragent.i18n import text
from foragent.order_prepare import _envelope_text, _now, _write
from foragent.order_prepare_cycle import HOLD_LIMIT, holds, other_issues
from foragent.order_prepare_prompt import _references, _risk_notes

REVIEW_TIMEOUT_S = 30 * 60
REPLY_SHAPE = ('{"verdict": "pass|hold|question", "reason": "<why>", "questions": [], '
               '"coverage": [{"statement": "<exact statement>", "tests": ["<test>"], '
               '"break_it": ["<wrong value / skipped effect / doubled effect evidence>"]}], '
               '"red": [{"command": "<command>", "collected": 0, "failed": 0, "exit_code": 0, '
               '"output": "<excerpt>", "failure_kind": "<your reading of why it failed>"}]}')


def _reply(message: str) -> dict | None:
    """The reviewer's JSON object, or None when it is missing or not the agreed shape."""
    for value in _envelope_text(message):
        if not isinstance(value, dict) or value.get("verdict") not in {"pass", "hold", "question"}:
            continue
        reason, questions = value.get("reason"), value.get("questions", [])
        coverage, red = value.get("coverage", []), value.get("red", [])
        if (isinstance(reason, str) and isinstance(questions, list) and all(isinstance(q, str) for q in questions)
                and isinstance(coverage, list) and all(isinstance(c, dict) for c in coverage)
                and isinstance(red, list) and all(isinstance(r, dict) for r in red)):
            return value
    return None


def _place(tree: Path, written: dict[str, str]) -> None:
    """Add the recorded candidates to a new scratch tree, never through a link."""
    for name, content in written.items():
        order_scratch._own_parents(tree, name, create=True)
        descriptor = os.open(tree / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o644)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content.encode("utf-8"))


def _integrity(git, tree: Path, manifest: dict, config: Config, protected, before, written: dict[str, str]) -> list[str]:
    """Paths of the review tree that are not current main plus exactly the recorded candidates.

    The writer's comparison (``order_scratch.changes``) is reused: any changed, removed or touched
    file outside the candidates, and any candidate whose bytes differ, means the reviewer's runs
    were not of current main with these tests.
    """
    candidates, refused, _output = order_scratch.changes(git, tree, manifest, config.acceptance_dir, protected, before)
    wrong = {name for name in candidates.keys() | written.keys()
             if candidates.get(name) != (written[name].encode("utf-8") if name in written else None)}
    return sorted(set(refused) | wrong)


def review_prompt(plan: dict, item: dict, config: Config, root: Path, entry: dict) -> str:
    """What the independent reviewer reads: statements, candidate bytes, commands, contracts, risks."""
    statements = "\n".join(f"- {line}" for line in item["statements"])
    candidates = "".join(f"\n### {name}\n```\n{content}\n```\n" for name, content in sorted(entry["written"].items()))
    commands = "".join(f"- `{command}`\n" for command in entry.get("done_when", [])) or "(no command was given)\n"
    listed = lambda values: "\n".join(f"- {value}" for value in values or []) or "(none)"  # noqa: E731
    notes = _risk_notes(root, config, item) or "(no risk map entry matches this issue)"
    earlier = "".join(f"- {reason}\n" for reason in holds(entry)) or "(none)\n"
    return (
        "You are the independent reviewer of acceptance tests that another session (the issue's lane)\n"
        "wrote before publication. You did not write them. Do not edit any file: read, run, judge, reply.\n\n"
        f"The working directory is a scratch copy of current {config.base_branch} "
        f"({entry.get('base_revision')}) with the candidate tests added. Run every command below here,\n"
        "in this sandbox (only this tree is writable, there is no network); the actual results are the\n"
        "current-main evidence. Never run them anywhere else.\n\n"
        "Pass only when all of these hold:\n"
        "- Every behaviour statement is covered by at least one actually collected test that goes\n"
        "  through the project's public entry points and fails on current main by an assertion about\n"
        "  the missing observable behaviour. Assertion failures are evidence. Collection, import or\n"
        "  syntax errors, zero collected tests, runs where everything is skipped or xfailed,\n"
        "  unconditional failures, timeouts and missing dependencies are not evidence.\n"
        "- The break-it rule: a wrong value, a skipped effect and the effect done twice (doubled)\n"
        "  must each make the tests fail.\n"
        "- The tests add no invented internal requirement (private helper names, internal layout),\n"
        "  ignore no statement, change no other issue's tests and weaken no shared test infrastructure.\n"
        "- The risk guidance is followed (process-control behaviour uses fake process tables only).\n"
        "Hold, with a reason the writer can act on, when the tests fall short. Ask a question only when\n"
        "a statement itself is unclear or cannot be observed; planning then answers it.\n\n"
        f"Reply with one JSON object only: {REPLY_SHAPE}\n"
        "coverage maps each statement, with its exact text, to the tests and your break-it evidence;\n"
        "red records every command below as you ran it: actual counts, exit code, an output excerpt and\n"
        "your reading of why it failed.\n\n"
        f"# Order: {plan['title']}\n{plan.get('summary', '')}\n\n## Spec\n{plan.get('spec_markdown', '')}\n\n"
        f"New lanes in this order (not created yet): "
        f"{json.dumps(plan.get('new_lanes', []), ensure_ascii=False, sort_keys=True)}\n\n"
        f"# Issue {item['key']}: {item['title']}\nLane: {item['lane']}\n\n"
        f"## Behaviour statements\n{statements}\n\n## Goal\n{item['goal']}\n\n"
        f"## Out of scope\n{item['out_of_scope']}\n\n## Checklist\n{listed(item.get('checklist'))}\n\n"
        f"## Blocked by\n{listed(item.get('blocked_by'))}\n\n"
        f"## Files expected to change later\n{listed(item.get('files'))}\n\n"
        f"## Candidate tests and helpers{candidates}\n"
        f"## Commands to run\n{commands}\n"
        "## Other issues of this order (contracts the tests may rely on)\nAnother issue's statements and goal\n"
        "are its public contract; its locked tests must not be changed.\n\n"
        f"{other_issues(plan, item)}\n\n"
        f"## Risk guidance\n{notes}\n\n## Earlier holds in this cycle\n{earlier}"
    )


def _options(reviewer: str, tree: Path, protected: tuple[Path, ...], config: Config, directory: Path) -> dict:
    """The writer's sandbox without its edit permission; no inherited hooks, MCP servers or user settings."""
    options = setup.permissions(reviewer, tree, protected, config.acceptance_dir, directory, edit=False)
    # Claude has no edit tool at all; Codex's sandbox keeps writes in the tree, checked after the review.
    return options | ({"read_only": True} if reviewer == "claude" else {})


def _outcome(result, reply: dict | None, item: dict, entry: dict, changed: list[str], writers: set[str],
             config: Config) -> dict:
    """The attempt's outcome. Only a well-formed verdict from an intact tree can be a hold or a pass;
    every other outcome carries no verdict, so it is never counted as a quality decision."""
    say = lambda key, **values: text(key, config.language, **values)  # noqa: E731
    if result.error_kind == "limit":
        return {"status": "waiting", "reason": say("prepare_review_limit")}
    if result.timed_out or result.exit_code != 0:
        return {"status": "interrupted", "reason": say(
            "prepare_review_failed", code=result.exit_code, timeout=", timed out" if result.timed_out else "",
            log=result.log_path)}
    if result.session_id and result.session_id in writers:
        return {"status": "interrupted", "reason": say("prepare_review_same_session")}
    if changed:
        return {"status": "interrupted", "reason": say("prepare_review_changed", paths=", ".join(changed))}
    if reply is None:
        return {"status": "interrupted", "reason": say("prepare_review_bad_reply")}
    facts = {"verdict": reply["verdict"], "coverage": reply.get("coverage", []), "red": reply.get("red", [])}
    if reply["verdict"] == "question":
        questions = [question for question in reply["questions"] if question.strip()]
        if not questions and not reply["reason"].strip():
            return {"status": "interrupted", "reason": say("prepare_review_incomplete", missing="questions")}
        return facts | {"status": "question", "questions": questions or [reply["reason"]],
                        "reason": say("prepare_review_question")}
    if not reply["reason"].strip():
        return {"status": "interrupted", "reason": say("prepare_review_incomplete", missing="reason")}
    if reply["verdict"] == "hold":
        return facts | {"status": "hold", "reason": reply["reason"]}
    if (missing := _evidence_gaps(item, entry, facts)):
        return {"status": "interrupted", "reason": say("prepare_review_incomplete", missing="; ".join(missing))}
    # The pass rests on every command failing now by collected, failing tests; reported facts saying
    # otherwise contradict it.
    runs = {run["command"]: run for run in facts["red"]}
    unproven = [command for command in entry["done_when"] if runs[command]["exit_code"] == 0
                or runs[command]["collected"] <= 0 or runs[command]["failed"] <= 0]
    if unproven:
        return facts | {"verdict": "hold", "status": "hold", "reason": say(
            "prepare_review_not_red", commands="; ".join(unproven))}
    return facts | {"status": "reviewed", "reason": reply["reason"]}


def _text(value) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _number(value) -> bool:
    return type(value) is int


def _evidence_gaps(item: dict, entry: dict, facts: dict) -> list[str]:
    """What a pass lacks of the agreed evidence: each statement mapped to written tests with its
    break-it evidence, and each command with its actual counts, exit code, output and failure kind."""
    names = set(entry["written"])
    names_test = lambda test: _text(test) and test.split("::", 1)[0] in names  # noqa: E731
    gaps = []
    for line in item["statements"]:
        covers = [cover for cover in facts["coverage"] if cover.get("statement") == line]
        if not any(isinstance(cover.get("tests"), list) and cover["tests"] and all(map(names_test, cover["tests"]))
                   and isinstance(cover.get("break_it"), list) and cover["break_it"]
                   and all(map(_text, cover["break_it"])) for cover in covers):
            gaps.append(f"coverage of {line!r} (tests among the written files and break-it evidence)")
    commands = entry.get("done_when", [])
    if not commands:
        gaps.append("done_when commands")
    for command in commands:
        runs = [run for run in facts["red"] if run.get("command") == command]
        if len(runs) != 1 or not (all(_number(runs[0].get(name)) for name in ("collected", "failed", "exit_code"))
                                  and _text(runs[0].get("output")) and _text(runs[0].get("failure_kind"))):
            gaps.append(f"one red run of {command!r} (collected, failed, exit_code, output, failure_kind)")
    return gaps


def review(plan: dict, item: dict, root: Path, config: Config, git, path: Path, record: dict) -> None:
    """One review attempt of the recorded candidates; the record is saved before and after it.

    Every attempt gets a new scratch copy of the candidates' base with exactly the recorded
    candidates, so nothing an earlier, interrupted reviewer changed reaches this one; the tree is
    checked whole after the reviewer, before its verdict is read.
    """
    say = lambda key, **values: text(key, config.language, **values)  # noqa: E731
    entry = record["issues"][item["key"]]
    number = len(entry["attempts"]) + 1
    directory = Path(entry["scratch"]).parent / f"review-{number}"
    tree = directory / "tree"
    facts = {"attempt": number, "role": "reviewer", "cycle": entry.get("cycle"), "started_at": _now(),
             "base_revision": entry.get("base_revision"), "scratch": str(tree),
             "log": str(directory / "reviewer.log")}
    entry["attempts"].append(facts)
    entry.update(status="reviewing_tests", phase="reviewing_tests", questions=[], updated_at=facts["started_at"])
    _write(path, record)
    outcome = {"status": "interrupted", "reason": say("prepare_review_stopped")}
    try:
        lane_config, context = setup.lane_context(plan, item, config, root)
        context.files.update(_references(plan, item))
        from foragent.run_implementation import lane_config as resolve

        lane_config = resolve(lane_config, item["lane"])
        reviewer, model = resolved_reviewer(lane_config), lane_config.reviewer_model
        facts.update(executor=reviewer, model=model)
        protected = order_scratch.protected(git, root, path.parent.parent)
        if directory.exists():
            raise ValueError(f"review scratch {directory} already exists")
        manifest = order_scratch.build(git, entry["base_revision"], tree, context)
        if context.refused:
            outcome = {"status": "interrupted", "reason": say("prepare_review_changed", paths=", ".join(context.refused))}
            return
        _place(tree, entry["written"])
        before = order_scratch.stamps(tree, manifest)
        writers = {attempt["session_id"] for attempt in entry["attempts"]
                   if attempt.get("role") == "writer" and attempt.get("session_id")}
        result = executor_for(reviewer).run(
            worktree=tree, prompt=review_prompt(plan, item, lane_config, root, entry), model=model,
            timeout_s=REVIEW_TIMEOUT_S, log_path=Path(facts["log"]),
            **_options(reviewer, tree, protected, lane_config, directory))
        facts.update(exit_code=result.exit_code, timed_out=result.timed_out, session_id=result.session_id)
        changed = _integrity(git, tree, manifest, lane_config, protected, before, entry["written"])
        outcome = _outcome(result, _reply(result.last_message or ""), item, entry, changed, writers, config)
    except OSError as exc:
        outcome = {"status": "waiting", "reason": say("prepare_review_error", error=" ".join(str(exc).split())[:300])}
    except (GitError, ValueError) as exc:
        outcome = {"status": "interrupted", "reason": say("prepare_review_error", error=" ".join(str(exc).split())[:300])}
    finally:
        facts.update(outcome, finished_at=_now())
        if outcome["status"] == "hold" and len(holds(entry)) >= HOLD_LIMIT:
            outcome = {"status": "question", "reason": say("prepare_review_held"),
                       "questions": [say("prepare_review_hold_question", count=len(holds(entry)),
                                         reasons=" | ".join(holds(entry)))]}
        entry.update(status=outcome["status"], questions=outcome.get("questions", []), reason=outcome["reason"],
                     updated_at=facts["finished_at"],
                     phase="writing_tests" if outcome["status"] == "hold" else "reviewing_tests")
        for name in ("coverage", "red"):
            entry[name] = outcome.get(name, [])
        _write(path, record)
