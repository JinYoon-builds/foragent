"""Acceptance test for order-perspectives. Locked by Foragent; do not edit."""

from collections import Counter
from dataclasses import replace
import json
from pathlib import Path
import re
import shutil
import socket
import subprocess
from threading import Barrier, Lock

import pytest
import yaml

from test_orch_review_handoff import bind_cli, make_case
from test_skills_autosync import cli_exit, isolated_home


# Public contract chosen here: top-level YAML `perspectives: {seat: executor}`;
# JSON {plan_hash, questions: [{id, perspective, executor, text}], failures: [...]};
# stats {perspectives: {seat: {asked, changed}}}. Executor replies use preread's
# one-question-per-line convention. Duplicate selection belongs to the interviewer.
SEATS = {"skeptic", "first_time_user", "attacker", "operator", "maintainer", "incidents"}
QUESTION = ("What would you ask that the person has not thought of, "
            "and how does this fail from your seat?")
COMMON = "Should recovery preserve the person's selected locale?"
EXECUTOR_ERROR = "Offline perspective executor failed to produce an answer"
RISK_MAP = "risks:\n  - id: unattended-recovery\n    failure: quota exhaustion loses an unfinished order\n"
DECISIONS = "# Decisions\n\nD71: Ask the person only about behaviour, scope or approval.\n"


def tree(root):
    return {str(path.relative_to(root)): path.read_bytes()
            for path in root.rglob("*") if path.is_file()}


@pytest.fixture
def perspectives_case(git_history, tmp_path, monkeypatch, isolated_home):
    from foragent import cli, executors, order
    from foragent.config import load_config

    case = make_case(git_history, tmp_path / "runner", monkeypatch,
                     language="en", skill_libraries=[], test_command="true")
    case.main = bind_cli(case, monkeypatch)
    # Exercise YAML loading, including the new key, rather than injecting an
    # attribute that the real configuration loader might reject or discard.
    monkeypatch.setattr(cli, "load_config", load_config)
    case.settings = {"repo": "owner/repo", "language": "en", "skill_libraries": []}
    (case.root / ".foragent.yml").write_text(yaml.safe_dump(case.settings), encoding="utf-8")
    case.plan = {
        "id": "2026-10-24-recovery", "title": "Resume an unfinished order",
        "summary": "Keep the selected locale across recovery.",
        "spec_markdown": "# Recovery\nResume after sleep without losing the selected locale.\n",
        "new_lanes": [], "edits": [], "issues": [
            {"key": "save", "title": "Save pending work", "goal": "Persist the unfinished order.",
             "out_of_scope": "Payments.", "done_when": ["test -s pending.json"],
             "checklist": [], "lane": "main", "size": "s", "blocked_by": [],
             "acceptance_files": {"tests/acceptance/test_recovery.py":
                                  "def test_recovery():\n    assert False, 'retain locale'\n"}},
            {"key": "resume", "title": "Resume pending work", "goal": "Recover after machine sleep.",
             "out_of_scope": "Changing the account.", "done_when": ["test -s recovered.json"],
             "checklist": [], "lane": "main", "size": "s", "blocked_by": ["save"],
             "acceptance_files": {}},
        ],
    }
    case.path = case.root / "PLAN.json"
    case.path.write_text(json.dumps(case.plan), encoding="utf-8")
    (case.root / "docs").mkdir()
    (case.root / "docs/decisions.md").write_text(DECISIONS, encoding="utf-8")
    (case.root / "feature.txt").write_text("Uncommitted work to preserve.\n", encoding="utf-8")
    (case.root / "CLAUDE.local.md").write_text("Existing local instructions.\n", encoding="utf-8")
    case.available = {"claude", "codex"}
    case.invocations, case.factories = [], []
    case.barrier, case.failure = None, None
    lock = Lock()

    def answer(executor, kwargs):
        with lock:
            number = len(case.invocations)
            record = {"executor": executor, "kwargs": kwargs, "error": number == 0 and case.failure,
                      "texts": [COMMON, f"Which recovery behaviour should scenario {number} use?"],
                      "overlapped": False}
            case.invocations.append(record)
        if case.barrier is not None:
            case.barrier.wait()
            record["overlapped"] = True
        if record["error"] == "exception":
            raise OSError(EXECUTOR_ERROR)
        return EXECUTOR_ERROR if record["error"] else "\n".join(record["texts"])

    # Reuse make_case's public Executor.run fake, including .steps and .calls,
    # just as test_order_preread.py does; never call either installed model CLI.
    case.workers = {}
    for name in ("claude", "codex"):
        worker = type(case.implementer)([lambda kw, name=name: answer(name, kw)] * 30)
        run = worker.run

        def run_with_error(*, _run=run, **kwargs):
            result = _run(**kwargs)
            return replace(result, exit_code=7) if result.last_message == EXECUTOR_ERROR else result

        worker.run = run_with_error
        case.workers[name] = worker

    def factory(name):
        with lock:
            case.factories.append(name)
        assert name in case.workers, f"Unexpected perspective executor: {name}"
        if name not in case.available:
            raise FileNotFoundError(f"{name} is unavailable offline")
        return case.workers[name]

    monkeypatch.setattr(executors, "executor_for", factory)
    for module in (cli, order):
        if hasattr(module, "executor_for"):
            monkeypatch.setattr(module, "executor_for", factory)
    original_which = shutil.which

    def which(name, *args, **kwargs):
        if name in case.workers:
            return f"/offline/bin/{name}" if name in case.available else None
        return original_which(name, *args, **kwargs)

    monkeypatch.setattr(shutil, "which", which)
    popen = subprocess.Popen

    def offline_process(argv, *args, **kwargs):
        assert isinstance(argv, (list, tuple)) and Path(argv[0]).name == "git", (
            "Perspectives must use the fake executors, without network, MCP or other processes", argv)
        return popen(argv, *args, **kwargs)

    def forbidden_network(*args, **kwargs):
        pytest.fail("Order perspectives must stay offline and must not contact GitHub")

    monkeypatch.setattr(subprocess, "Popen", offline_process)
    monkeypatch.setattr(socket.socket, "connect", forbidden_network)
    monkeypatch.setattr(socket, "create_connection", forbidden_network)
    monkeypatch.setattr(cli, "GhTracker", forbidden_network)
    return case


def invoke(case, capsys, *args, json_output=True):
    capsys.readouterr()
    argv = ["order", "perspectives", *map(str, args)]
    if json_output:
        argv.append("--json")
    code = cli_exit(case.main, argv)
    captured = capsys.readouterr()
    assert code == 0, f"fa {' '.join(argv)} exited {code}: {captured.err.strip()} {captured.out.strip()}"
    return json.loads(captured.out) if json_output else captured.out


def run_perspectives(case, capsys):
    before, plan_before = tree(case.root), case.path.read_bytes()
    start = len(case.invocations)
    payload = invoke(case, capsys, case.path)
    assert case.path.read_bytes() == plan_before, "Perspectives must not rewrite the forming plan"
    assert tree(case.root) == before, "Read-only perspectives must leave the entire worktree unchanged"
    assert isinstance(payload.get("plan_hash"), str) and payload["plan_hash"]
    assert isinstance(payload.get("questions"), list) and isinstance(payload.get("failures"), list)
    for call in case.invocations[start:]:
        assert call["kwargs"].get("read_only") is True, "Executor.run must receive read_only=True"
        assert Path(call["kwargs"]["worktree"]).resolve() == case.root.resolve()
    return payload, case.invocations[start:]


def assert_questions(payload, calls, *, failed=0):
    questions = payload["questions"]
    assert len(calls) == len(SEATS), "Run one agent per perspective, not one per issue"
    assert len(questions) == 2 * (len(SEATS) - failed), "Keep every successful agent's questions"
    assert len(payload["failures"]) == failed
    ids = [question["id"] for question in questions]
    assert all(isinstance(identity, str) and identity for identity in ids)
    assert len(set(ids)) == len(ids), "Every question needs its own markable id, including duplicates"
    assert Counter(question["text"] for question in questions)[COMMON] == len(SEATS) - failed, (
        "Return all seats' evidence; the interviewer merges duplicate questions")
    accounted = set()
    for seat in SEATS - {failure["perspective"] for failure in payload["failures"]}:
        group = [question for question in questions if question["perspective"] == seat]
        assert len(group) == 2, f"Missing questions from {seat}"
        call = next((call for call in calls if not call["error"]
                     and set(call["texts"]) == {question["text"] for question in group}), None)
        assert call is not None, f"Questions from {seat} must preserve the executor's actual answer"
        assert all(question["executor"] == call["executor"] for question in group)
        prompt = " ".join(call["kwargs"]["prompt"].casefold().replace("_", " ").replace("-", " ").split())
        assert seat.replace("_", " ") in prompt, f"The agent needs its {seat} seat in the prompt"
        accounted.add(id(call))
    assert len(accounted) == len(SEATS) - failed


@pytest.mark.parametrize("risk_map_present", [True, False], ids=["with-risk-map", "without-risk-map"])
def test_order_perspectives_default_seats_run_in_parallel_read_only(perspectives_case, capsys, risk_map_present):
    case = perspectives_case
    if risk_map_present:
        (case.root / ".foragent-risks.yml").write_text(RISK_MAP, encoding="utf-8")
    # All six calls must enter before any returns. A sequential implementation
    # breaks this bounded barrier; elapsed-time thresholds would be flaky.
    case.barrier = Barrier(len(SEATS), timeout=5)
    payload, calls = run_perspectives(case, capsys)
    assert_questions(payload, calls)
    assert all(call["overlapped"] for call in calls), "Perspective agents must run in parallel"
    counts = Counter(call["executor"] for call in calls)
    assert counts["codex"] >= 2 and counts["claude"] == len(SEATS) - counts["codex"] > 0, (
        "With both tools available, put at least two default seats on Codex and the rest on Claude")
    for call in calls:
        prompt = call["kwargs"]["prompt"]
        assert QUESTION.casefold() in " ".join(prompt.casefold().split())
        content = [case.plan[key] for key in ("id", "title", "summary", "spec_markdown")]
        for issue in case.plan["issues"]:
            content += [issue["goal"], issue["out_of_scope"], *issue["done_when"],
                        *issue["acceptance_files"], *issue["acceptance_files"].values()]
        for text in content:
            assert text in prompt or json.dumps(text)[1:-1] in prompt, f"Perspective omitted plan content: {text!r}"
    prompts = {seat: next(call["kwargs"]["prompt"] for call in calls
                          if any(question["perspective"] == seat and question["text"] == call["texts"][1]
                                 for question in payload["questions"])) for seat in SEATS}
    assert "simpler" in prompts["skeptic"].casefold()
    assert "abuse" in prompts["attacker"].casefold()
    assert all(word in prompts["operator"].casefold() for word in ("sleep", "quota", "fail"))
    assert re.search(r"(?:six|6) months", prompts["maintainer"], re.IGNORECASE)
    assert DECISIONS.strip() in prompts["incidents"], "Incidents needs the repository's decisions, not just a path"
    if risk_map_present:
        assert RISK_MAP.strip() in prompts["incidents"], "Incidents needs the repository's actual risk map"


def test_order_perspectives_honors_per_perspective_executor_config(perspectives_case, capsys):
    case = perspectives_case
    # One Codex seat deliberately differs from the default's >=2, while still
    # requiring distinct executors in the same run rather than a global toggle.
    configured = {seat: "codex" if seat == "incidents" else "claude" for seat in SEATS}
    case.settings["perspectives"] = configured
    (case.root / ".foragent.yml").write_text(yaml.safe_dump(case.settings), encoding="utf-8")
    payload, calls = run_perspectives(case, capsys)
    assert_questions(payload, calls)
    assert {question["perspective"]: question["executor"] for question in payload["questions"]} == configured
    assert Counter(call["executor"] for call in calls) == {"codex": 1, "claude": 5}


@pytest.mark.parametrize("unavailable", ["codex", "claude"])
def test_order_perspectives_unavailable_executor_falls_back(perspectives_case, capsys, unavailable):
    case = perspectives_case
    case.available.remove(unavailable)
    payload, calls = run_perspectives(case, capsys)
    assert_questions(payload, calls)
    assert {question["executor"] for question in payload["questions"]} == case.available
    assert {call["executor"] for call in calls} == case.available
    assert case.workers[unavailable].calls == [], "Never run an unavailable executor"


@pytest.mark.parametrize("failure", ["nonzero", "exception"])
def test_order_perspectives_one_executor_error_preserves_other_questions(perspectives_case, capsys, failure):
    case = perspectives_case
    case.failure = failure
    payload, calls = run_perspectives(case, capsys)
    assert_questions(payload, calls, failed=1)
    failed_call = next(call for call in calls if call["error"])
    error = payload["failures"][0]
    assert error["perspective"] in SEATS and error["executor"] == failed_call["executor"]
    prompt = failed_call["kwargs"]["prompt"].casefold().replace("_", " ").replace("-", " ")
    assert error["perspective"].replace("_", " ") in prompt
    assert EXECUTOR_ERROR in error["error"], "List the failed seat's executor error without discarding successful seats"
    stats = invoke(case, capsys, "stats")["perspectives"]
    assert sum(row["asked"] for row in stats.values()) == 10, "A failed agent did not ask questions"


def objects(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from objects(child)
    elif isinstance(value, list):
        for child in value:
            yield from objects(child)


def test_order_perspectives_content_keyed_ledger_marks_and_stats_survive_reload(perspectives_case, capsys):
    from foragent.state import StateStore

    case = perspectives_case
    original, _ = run_perspectives(case, capsys)
    copied = case.root / "copied-PLAN.json"
    copied.write_bytes(case.path.read_bytes())
    case.path = copied
    same_content, _ = run_perspectives(case, capsys)
    assert same_content["plan_hash"] == original["plan_hash"], "The ledger follows content, not the plan's filename"
    case.plan["spec_markdown"] += "Recovery must also preserve the selected time zone.\n"
    case.path.write_text(json.dumps(case.plan), encoding="utf-8")
    changed_content, _ = run_perspectives(case, capsys)
    assert changed_content["plan_hash"] != original["plan_hash"], "Reusing an order id must not conflate different specs"
    assert {question["id"] for question in original["questions"]}.isdisjoint(
        question["id"] for question in changed_content["questions"]), "mark ID must unambiguously identify its plan revision"
    questions = {question["id"]: question for result in (original, same_content, changed_content)
                 for question in result["questions"]}
    yes = next(question for question in original["questions"] if question["perspective"] == "skeptic")
    no = next(question for question in original["questions"]
              if question["perspective"] == "skeptic" and question["id"] != yes["id"])
    another_yes = next(question for question in changed_content["questions"] if question["perspective"] == "operator")
    before, calls_before = tree(case.root), len(case.invocations)
    initial = invoke(case, capsys, "stats")["perspectives"]
    assert all(row["changed"] == 0 for row in initial.values()), "Unmarked questions are not spec changes"
    for question, changed in ((yes, "yes"), (no, "no"), (another_yes, "yes"), (yes, "yes")):
        invoke(case, capsys, "mark", question["id"], "--changed", changed, json_output=False)
    # A fresh store must recover evidence from disk, including an older revision
    # after another plan has been examined. No process-local tally is sufficient.
    case.store = StateStore(case.store.root)
    stats = invoke(case, capsys, "stats")["perspectives"]
    assert set(stats) == SEATS
    asked = Counter(question["perspective"] for question in questions.values())
    for seat in SEATS:
        assert stats[seat]["asked"] == asked[seat]
        assert stats[seat]["changed"] == int(seat in {"skeptic", "operator"}), "Repeated marks must not inflate changes"
    documents = [(path, json.loads(path.read_text(encoding="utf-8")))
                 for path in case.store.root.rglob("*.json")]
    for result in (original, changed_content):
        digest = result["plan_hash"]
        assert any(digest == path.stem or digest in path.relative_to(case.store.root).parts
                   or any(digest in obj for obj in objects(value))
                   for path, value in documents), "Persist a ledger keyed by the plan's content hash under the state folder"
    saved = [obj for _, value in documents for obj in objects(value)]
    for question, changed in ((yes, True), (no, False), (another_yes, True)):
        identity = question["id"]
        assert any((obj.get("id") == identity and obj.get("changed") is changed)
                   or (isinstance(obj.get(identity), dict) and obj[identity].get("changed") is changed)
                   for obj in saved), f"Persist the explicit changed={changed} mark for {identity}"
    assert len(case.invocations) == calls_before, "mark and stats must only use the ledger, without model calls"
    assert tree(case.root) == before, "mark and stats must not edit the plan or worktree"


def test_order_perspectives_skill_questions_formed_spec_before_lock_and_keeps_preread():
    path = Path(__file__).resolve().parents[2] / "skills/fa-order/SKILL.md"
    text = " ".join(path.read_text(encoding="utf-8").casefold().split())
    perspectives = text.find("fa order perspectives plan.json")
    preread = text.find("fa order preread plan.json")
    assert perspectives >= 0, "fa-order must run perspectives on the roughly formed spec before locking"
    assert perspectives < preread, "Perspective questions precede the separate implementer preread step"
    # Read the instruction's section, without prescribing a heading or step number.
    raw = path.read_text(encoding="utf-8").casefold()
    position = raw.index("fa order perspectives plan.json")
    start = raw.rfind("\n##", 0, position)
    end = raw.find("\n##", position)
    section = " ".join(raw[max(0, start):end if end >= 0 else len(raw)].split())
    assert re.search(r"(?:roughly|mostly|formed|taking shape).{0,100}spec|spec.{0,100}(?:roughly|mostly|formed|taking shape)", section)
    assert re.search(r"before.{0,100}lock", section), "Tell the interviewer when to challenge the spec"
    assert re.search(r"(?:merge|combine|deduplicate).{0,80}duplicate", section), "The interviewer merges duplicates"
    assert "d71" in section and all(word in section for word in ("scope", "approval"))
    assert "behaviour" in section or "behavior" in section
    assert re.search(r"ask.{0,100}(?:person|user).{0,100}only|only.{0,100}ask.{0,100}(?:person|user)", section)
    assert "fa order perspectives mark" in section and "--changed" in section
    assert re.search(r"(?:each|every).{0,80}question|question.{0,80}(?:each|every)", section)
    assert "fa order preread plan.json --json" in text and "unknown" in text
