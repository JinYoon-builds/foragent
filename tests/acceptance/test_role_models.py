"""Acceptance test for role-models (#1473). Locked by Foragent; do not edit.

Each role session runs on a model chosen by the weight of its work. The project's tier ladder names
the models: the size-l tier is the best model and the size-m tier the next one. The architect's
design checks (the orchestrator's `fa orch architect --why`) use the best model; its light check after
an order finishes and every docs-owner session use the next one. A per-role model setting wins.
"""

from dataclasses import replace
from datetime import timedelta

import yaml

from foragent import scheduler
from test_architect_project_schedule import complete, review_case  # noqa: F401
from test_orch_architect import architect_case, call_architect, finish_session
from test_orch_inbox import inbox_command
from test_orch_review_handoff import tick_context
from test_skills_autosync import cli_exit
from test_architect_project_schedule import fake_workloads, isolated_home, offline_github  # noqa: F401


BEST, NEXT, SMALL = "ladder-best-model", "ladder-next-model", "ladder-small-model"
TIERS = {
    "s": {"codex_model": "codex-s", "codex_effort": "medium", "claude_model": SMALL, "claude_effort": "low"},
    "m": {"codex_model": "codex-m", "codex_effort": "high", "claude_model": NEXT, "claude_effort": "medium"},
    "l": {"codex_model": "codex-l", "codex_effort": "xhigh", "claude_model": BEST, "claude_effort": "high"},
}


def ladder_case(tmp_path, monkeypatch, roles=None):
    case = architect_case(tmp_path, monkeypatch)
    config = {"repo": "owner/repo", "language": "en", "skill_libraries": [], "notify": "none",
              "executor": "claude", "tiers": TIERS, **({"roles": roles} if roles else {})}
    (case.root / ".foragent.yml").write_text(yaml.safe_dump(config, sort_keys=False))
    return case


def run_role(case, capsys, role):
    capsys.readouterr()
    code = inbox_command(case, "role", role, "--why", f"{role}: two merged orders changed the merge path")
    output = capsys.readouterr()
    assert code == 0, f"fa orch role {role} --why must start a session: {output.err.strip()}"
    child = case.launches[-1]
    child.returncode = cli_exit(case.main, child.args[3:])
    assert child.returncode == 0
    return case.turns[-1]


def test_design_check_uses_the_best_model_and_the_docs_owner_the_next(tmp_path, monkeypatch, capsys):
    case = ladder_case(tmp_path, monkeypatch)
    finish_session(case, call_architect(case, capsys))
    assert case.turns[0]["model"] == BEST, "An architect design check runs on the best model"
    docs = run_role(case, capsys, "docs")
    assert docs["model"] == NEXT, "The docs owner runs on the next model, not the best one"
    assert [turn["model"] for turn in case.turns] == [BEST, NEXT], "One turn per session, each on its own model"


def test_per_role_model_setting_wins_for_that_role_only(tmp_path, monkeypatch, capsys):
    roles = {name: {"agent": f"fa-{name}", "skills": [f"fa-{name}"],
                    "memory": f".claude/agent-memory/fa-{name}/", "signals": []}
             for name in ("orchestrator", "architect", "docs")}
    roles["docs"]["model"] = "docs-own-model"
    case = ladder_case(tmp_path, monkeypatch, roles=roles)
    finish_session(case, call_architect(case, capsys))
    assert case.turns[0]["model"] == BEST, "Another role's setting does not change the architect's model"
    assert run_role(case, capsys, "docs")["model"] == "docs-own-model", (
        "The docs role's own model setting decides its model")


def test_the_architect_light_check_after_an_order_finishes_uses_the_next_model(review_case):
    case = review_case
    case.ctx.config = replace(case.ctx.config, executor="claude", tiers=TIERS)
    case.clock[0] += timedelta(minutes=1)
    assert scheduler.tick(tick_context(case)).skipped is None
    assert len(case.launches) == 1, "A finished order wakes the architect's light check"
    complete(case)
    assert len(case.turns) == 1
    assert case.turns[0]["model"] == NEXT, "The light per-merge-group check runs on the next model"
