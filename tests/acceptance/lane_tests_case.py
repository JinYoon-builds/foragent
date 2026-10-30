"""Offline public-boundary fixtures for the lane-writes-tests order.

The executor response envelopes are the shared preparation-agent contract in the
order spec. No real agent, GitHub call, signal, commit or push is used here.
"""

from dataclasses import replace
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest
import yaml

from conftest import git_command
from foragent import executors
from foragent.executors import ExecResult
from foragent.state import StateStore
from test_order import offline_publication
from test_publish_out_of_scope import publish_cli
from test_skills_autosync import cli_exit


TEST_PATH = "tests/acceptance/test_notice.py"
STATEMENT = "Sending a notice reports one delivery and writes exactly one receipt."
NOTICE = 'import json\nprint(json.dumps({"deliveries": 0}))\n'
CANDIDATE = '''import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class Delivery(unittest.TestCase):
    def test_notice_is_delivered_once(self):
        source = Path(__file__).resolve().parents[2] / "notice.py"
        with tempfile.TemporaryDirectory() as output:
            receipt = Path(output) / "receipt.txt"
            result = subprocess.run([sys.executable, str(source), "send"], cwd=output,
                                    capture_output=True, text=True, check=True)
            self.assertEqual(json.loads(result.stdout)["deliveries"], 1)
            self.assertTrue(receipt.is_file(), "sending must write a receipt")
            self.assertEqual(receipt.read_text().splitlines(), ["delivered"])


if __name__ == "__main__":
    unittest.main()
'''
CHECK = f"{sys.executable} {TEST_PATH}"


@pytest.fixture
def lane_case(offline_publication, git_history, tmp_path, monkeypatch):
    root, config, tracker, git, git_calls, origin = offline_publication
    git_history.update({"notice.py": NOTICE})
    git_command(root, "fetch", "origin", "main")
    git_command(root, "restore", "--source=origin/main", "--staged", "--worktree", "--", ".")
    # Move only this synthetic repo's ref, without any prohibited Git command.
    git_command(root, "update-ref", "refs/heads/main", git_history.tip)
    config = replace(config, repo="owner/repo", language="en", executor="codex",
                     executor_model="writer-model", reviewer="claude",
                     reviewer_model="reviewer-model", skill_libraries=[],
                     lanes={"api": {"paths": ["notice.py"], "agent": "fa-api",
                                    "model": "lane-writer-model"}})
    (root / ".foragent.yml").write_text(yaml.safe_dump({
        "repo": "owner/repo", "language": "en", "executor": "codex",
        "executor_model": "writer-model", "reviewer": "claude",
        "reviewer_model": "reviewer-model", "skill_libraries": [], "lanes": config.lanes,
    }))
    (root / ".foragent-risks.yml").write_text(
        "- paths: [notice.py]\n  guidance: fake process tables only; never send real signals\n")
    agents = root / ".claude/agents"
    agents.mkdir(parents=True)
    (agents / "fa-api.md").write_text("---\nname: fa-api\n---\nLANE CONTEXT: receipt delivery\n")
    store = StateStore.for_repo("owner/repo")
    monkeypatch.setattr(StateStore, "for_repo", classmethod(lambda cls, repo, **kw: store))
    main = publish_cli((root, config, tracker, git, []), monkeypatch)
    monkeypatch.chdir(root)
    plan = {"id": "notice", "title": "Send notices", "summary": STATEMENT,
            "spec_markdown": "# Notice\nDeliver once, including the receipt.\n",
            "issues": [{"key": "notice", "title": "Notice delivery", "goal": STATEMENT,
                        "statements": [STATEMENT], "out_of_scope": "Delivery providers.",
                        "done_when": ["true"], "lane": "api", "size": "s",
                        "files": ["notice.py"], "blocked_by": []}]}
    case = SimpleNamespace(root=root, config=config, tracker=tracker, git=git,
                           git_calls=git_calls, main=main, plan=plan,
                           path=root / "PLAN.json", calls=[], verdicts=["pass"],
                           writer_questions=[], candidate=CANDIDATE, writer_hook=None,
                           reviewer_hook=None, writer_exit=0, reviewer_exit=0,
                           review_kind="assertion", store=store)

    def run(name, **kw):
        role = ("legacy" if name == "codex" and getattr(case, "action", None) == "preread"
                else "writer" if name == "codex" else "reviewer")
        case.calls.append({"role": role, "executor": name, **kw})
        worktree = Path(kw["worktree"])
        if role == "legacy":
            return ExecResult(0, False, "NONE", kw["log_path"])
        if role == "writer":
            path = worktree / TEST_PATH
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(case.candidate)
            if case.writer_hook:
                case.writer_hook(worktree)
            message = {"acceptance_files": {TEST_PATH: case.candidate},
                       "done_when": [CHECK], "questions": case.writer_questions}
            code = case.writer_exit
        else:
            case.calls[-1]["candidate"] = (worktree / TEST_PATH).read_text()
            # The reviewer observes a real run of the toy project's public check.
            # This uses unittest, not a nested pytest invocation or live service.
            checked = subprocess.run([sys.executable, TEST_PATH], cwd=worktree,
                                     capture_output=True, text=True)
            case.calls[-1]["check_result"] = checked
            if case.reviewer_hook:
                case.reviewer_hook(worktree)
            verdict = case.verdicts.pop(0) if case.verdicts else "pass"
            message = {"verdict": verdict, "reason": "Delivery observable once; break-it checked.",
                       "questions": ["Which delivery is meant?"] if verdict == "question" else [],
                       "coverage": [{"statement": STATEMENT, "tests": [TEST_PATH],
                                     "break_it": ["wrong value", "skipped effect", "doubled effect"]}],
                       "red": [{"command": CHECK, "collected": 1, "failed": 1,
                                "exit_code": checked.returncode,
                                "output": checked.stdout + checked.stderr,
                                "failure_kind": case.review_kind}]}
            code = case.reviewer_exit
        log = Path(kw["log_path"])
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(json.dumps(message))
        return ExecResult(code, False, json.dumps(message), log,
                          session_id=f"{role}-{len(case.calls)}",
                          error_kind="limit" if code and role == "reviewer" else "normal")

    for name, cls in (("codex", executors.CodexExecutor), ("claude", executors.ClaudeExecutor)):
        monkeypatch.setattr(cls, "run", lambda self, _name=name, **kw: run(_name, **kw))
    case.save = lambda: case.path.write_text(json.dumps(case.plan))
    case.save()
    return case


def invoke(case, capsys, *args):
    case.action = args[0]
    capsys.readouterr()
    code = cli_exit(case.main, ["order", *args, "--repo-root", str(case.root)])
    output = capsys.readouterr()
    return code, output.out, output.err


def prepare(case, capsys, *, status=False):
    args = ["prepare", str(case.path), "--json"] + (["--status"] if status else [])
    code, out, err = invoke(case, capsys, *args)
    assert code == 0, f"Preparation must report per-issue facts: {out}{err}"
    result = json.loads(out)
    assert len(result["issues"]) == len(case.plan["issues"]), result
    return result["issues"][0]


def publish(case, capsys, *options):
    return invoke(case, capsys, "publish", str(case.path), *options)


def assert_no_publication(case):
    assert case.tracker.issues == {}
    assert not any("commit" in call or "push" in call for call in case.git_calls)


def disk_plan(case):
    return json.loads(case.path.read_text())
