"""Locked acceptance for #1040: trusted discovery and observed skill use.

Run through the public issue runner with offline executor/GitHub fakes. Catalogs
may be inline, linked files, or native executor skill files. Selection belongs
to the lane. This file makes no claim about trials, retirement, or proposals.
"""

import json
from pathlib import Path

import pytest

from test_orch_review_handoff import make_case
from test_skills_autosync import isolated_home  # noqa: F401


def skill_case(git_history, tmp_path, monkeypatch, executor, *, bulk=0):
    trusted = tmp_path / "trusted"
    foreign = tmp_path / "foreign"
    descriptions = {
        "fixture-a7r": "Explain repeated review findings using their evidence",
        "fixture-b8s": "Design focused acceptance checks for a reported defect",
        "fixture-c9t": "The person's chosen product voice",
        "fixture-d2u": "A different product voice the person did not choose",
        **{f"reference-{n:03d}": f"Reference {n}: " + "Concrete library guidance. " * 20
           for n in range(bulk)},
    }
    for root, entries in ((trusted, descriptions), (foreign, {"fixture-e3v": "Unknown source"})):
        for name, description in entries.items():
            path = root / "skills" / name / "SKILL.md"
            path.parent.mkdir(parents=True)
            path.write_text(f"---\nname: {name}\ndescription: {json.dumps(description)}\n---\nUse evidence.\n")
        (root / "LICENSE").write_text("SPDX-License-Identifier: MIT\n")
    # Aliases are deliberately unrelated to trust. Trust is about the source.
    roles = {"approved-source:fixture-c9t": "style", "approved-source:fixture-d2u": "style",
             **{f"approved-source:reference-{n:03d}": "tool" for n in range(bulk)}}
    case = make_case(
        git_history, tmp_path, monkeypatch, executor=executor,
        reviewer="claude" if executor == "codex" else "codex",
        skill_libraries=[
            {"name": "approved-source", "repo": "https://github.com/anthropics/skills.git",
             "path": str(trusted)},
            {"name": "anthropics", "repo": "https://github.com/unknown-publisher/skills.git",
             "path": str(foreign)},
        ],
        skill_roles=roles,
        lanes={"api": {"paths": ["feature.txt"], "skills": ["fixture-c9t"]}},
    )
    case.store.update_run(1, lane="api")
    from foragent.executors import ExecResult
    case.reviewer.run = lambda **call: ExecResult(0, False,
        json.dumps({"result": "(none)"}) if call.get("review_level") is not None else "VERDICT: PASS",
        call["log_path"])
    return case, trusted, descriptions


def visible_catalog(call, sandbox):
    """Observe prompt/file boundaries without fixing a catalog filename or format.

    Follow any text file referenced by the prompt, transitively. Also admit the
    executor's documented native skill directories. Never scan the library as
    evidence of offering it: a library file must first be reachable by the lane.
    """
    worktree = call["worktree"]
    text = call["prompt"]
    native = [worktree / ".claude/skills", worktree / ".agents/skills"]
    candidates = [path for path in sandbox.rglob("*") if path.is_file() and ".git" not in path.parts]
    seen = set()
    while True:
        added = False
        for path in candidates:
            if path in seen:
                continue
            references = [str(path)]
            if path.is_relative_to(worktree):
                references.append(path.relative_to(worktree).as_posix())
            if not (any(ref in text for ref in references)
                    or any(path.is_relative_to(root) for root in native)):
                continue
            seen.add(path)
            try:
                text += "\n" + path.read_text()
            except (UnicodeError, OSError):
                continue
            added = True
        if not added:
            return text


@pytest.mark.parametrize("executor", ["codex", "claude"])
def test_trusted_source_is_available_without_a_role_and_unknown_source_is_absent(
    git_history, tmp_path, monkeypatch, executor,
):
    from foragent.runner import run_issue

    case, trusted, descriptions = skill_case(git_history, tmp_path, monkeypatch, executor)
    assert "approved-source:fixture-a7r" not in case.ctx.config.skill_roles
    observed = []

    def execute(call):
        observed.append(visible_catalog(call, tmp_path))
        return case.implement(call)

    case.implementer.steps = [execute]
    assert run_issue(1, case.ctx) == "merged"
    offered = observed[0]
    for name in ("fixture-a7r", "fixture-b8s"):
        assert name in offered and descriptions[name] in offered
        assert str(trusted / "skills" / name / "SKILL.md") in offered
    assert "fixture-e3v" not in offered


@pytest.mark.parametrize("executor", ["codex", "claude"])
def test_270_installed_skills_do_not_fill_executor_prompts(git_history, tmp_path, monkeypatch, executor):
    from foragent.runner import run_issue

    case, _, descriptions = skill_case(git_history, tmp_path, monkeypatch, executor, bulk=266)
    observed = []

    def execute(call):
        observed.append((call["prompt"], visible_catalog(call, tmp_path)))
        return case.implement(call)

    case.implementer.steps = [execute]
    assert run_issue(1, case.ctx) == "merged"
    # 24 KiB is about 6K English tokens for this tiny issue, leaving most of a
    # 32K context for work. The fixture's full descriptions alone exceed 130 KiB.
    for prompt, catalog in observed:
        assert len(prompt.encode()) <= 24 * 1024
        assert not all(f"reference-{n:03d}" in prompt for n in range(266))
        for name, description in descriptions.items():
            if name not in {"fixture-c9t", "fixture-d2u"}:
                assert name in catalog and description in catalog
        assert "fixture-e3v" not in catalog and "fixture-d2u" not in catalog


def read_events(executor, paths):
    for n, path in enumerate(paths):
        content = path.read_text()  # The fake executor really performs the read.
        if executor == "claude":
            yield {"type": "assistant", "message": {"content": [{
                "type": "tool_use", "id": f"read-{n}", "name": "Read", "input": {"file_path": str(path)},
            }]}}
            yield {"type": "user", "message": {"content": [{
                "type": "tool_result", "tool_use_id": f"read-{n}", "content": content,
            }]}}
        else:
            yield {"type": "item.completed", "item": {
                "id": f"read-{n}", "type": "command_execution", "command": f"cat '{path}'",
                "aggregated_output": content, "exit_code": 0, "status": "completed",
            }}


@pytest.mark.parametrize("executor", ["codex", "claude"])
@pytest.mark.parametrize("reads,report,expected", [
    (73, "Skills used: none", ["fixture-a7r", "fixture-b8s"]),
    (0, "Skills used: fixture-a7r", []),
    (1, "", ["fixture-a7r"]),
])
def test_usage_comes_from_executor_reads_even_when_the_report_disagrees(
    git_history, tmp_path, monkeypatch, executor, reads, report, expected,
):
    from foragent.runner import run_issue

    case, trusted, _ = skill_case(git_history, tmp_path, monkeypatch, executor)

    def execute(call):
        case.implement(call)
        names = ["fixture-a7r", "fixture-b8s"]
        paths = [trusted / "skills" / names[n % 2] / "SKILL.md" for n in range(reads)]
        events = list(read_events(executor, paths))
        failed = str(trusted / "skills/fixture-d2u/SKILL.md")
        if executor == "claude":
            events.extend([
                {"type": "assistant", "message": {"content": [{
                    "type": "tool_use", "id": "failed-read", "name": "Read", "input": {"file_path": failed},
                }]}},
                {"type": "user", "message": {"content": [{
                    "type": "tool_result", "tool_use_id": "failed-read", "is_error": True,
                    "content": "Read failed",
                }]}},
            ])
        else:
            events.append({"type": "item.completed", "item": {
                "type": "command_execution", "command": f"cat '{failed}'", "exit_code": 1,
                "aggregated_output": "Read failed", "status": "completed",
            }})
        # Merely mentioning another SKILL.md in prose is not a read.
        events.append({"type": "assistant", "message": {"content": [{
            "type": "text", "text": str(trusted / "skills/fixture-d2u/SKILL.md"),
        }]}})
        with call["log_path"].open("a") as stream:
            stream.write("\n".join(json.dumps(event) for event in events) + "\n")
        return "Implemented feature.\n" + report

    case.implementer.steps = [execute]
    assert run_issue(1, case.ctx) == "merged"
    assert sorted(case.store.get_run(1).skills_used) == sorted(expected)
    line, = [line for line in case.tracker.created_prs[0].body.splitlines() if line.startswith("Skills used:")]
    actual = line.partition(":")[2].strip()
    assert sorted(actual.split(", ")) == sorted(expected or ["none"])


@pytest.mark.parametrize("executor", ["codex", "claude"])
def test_persons_fixed_style_survives_trusted_discovery(git_history, tmp_path, monkeypatch, executor):
    from foragent.runner import run_issue

    case, _, _ = skill_case(git_history, tmp_path, monkeypatch, executor)
    observed = []

    def execute(call):
        observed.append(visible_catalog(call, tmp_path))
        fixed = call["worktree"] / ".claude/skills/fixture-c9t/SKILL.md"
        assert fixed.is_file() and "The person's chosen product voice" in fixed.read_text()
        return case.implement(call)

    case.implementer.steps = [execute]
    assert run_issue(1, case.ctx) == "merged"
    assert case.ctx.config.lanes["api"]["skills"] == ["fixture-c9t"]
    assert "fixture-d2u" not in observed[0]
