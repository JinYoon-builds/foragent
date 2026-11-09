"""Offline publication world for order-accepts-existing-blocker (helper, not a test file).

Publishing runs through ``fa order publish`` (cli.main) into the fake tracker; no network,
no GitHub process.
"""

from copy import deepcopy
from dataclasses import replace
import json
import socket
from types import SimpleNamespace

import pytest

from test_local_truth import record_gh
from test_publish_out_of_scope import publish_cli
from test_publish_own_paths import with_clean_preread
from test_skills_autosync import cli_exit


OPEN_BLOCKER = 41      # an existing open issue a new issue waits on
EDITED = 42            # an existing issue an order edits
OTHER = 43             # an unrelated existing issue
MISSING = 977          # no issue with this number exists


def make_case(publishing, monkeypatch):
    root, config, tracker, git, events = publishing
    config = replace(config, language="en", executor="codex", skill_libraries=[])
    monkeypatch.chdir(root)
    calls = record_gh(monkeypatch)

    def no_network(*args, **kwargs):
        pytest.fail("Publication must use the offline tracker")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    tracker.add_issue(OPEN_BLOCKER, "Earlier published work", "The earlier work.")
    tracker.add_issue(EDITED, "Existing work to edit", "The previous decision.")
    tracker.add_issue(OTHER, "Unrelated work", "Unrelated.")
    main = with_clean_preread(
        publish_cli((root, config, tracker, git, events), monkeypatch), monkeypatch,
    )
    return SimpleNamespace(root=root, tracker=tracker, events=events, main=main, gh=calls,
                           path=root / "PLAN.json")


def new_issue(key="waiting", title="Build on the earlier work", blocked_by=()):
    return {
        "key": key, "title": title,
        "goal": "Build on the earlier published work.", "out_of_scope": "Other work.",
        "done_when": ["true"], "lane": "api", "size": "s",
        "acceptance_files": {}, "blocked_by": list(blocked_by),
    }


def edit(number=EDITED, blocked_by=()):
    return {
        "number": number, "goal": "Apply the decided change after the earlier work.",
        "out_of_scope": "Other work.", "done_when": ["true"], "checklist": [],
        "acceptance_files": {}, "blocked_by": list(blocked_by),
    }


def plan(order_id, issues=(), edits=()):
    return {
        "id": order_id, "title": "Wait on earlier work", "summary": "Work that waits on an existing issue.",
        "spec_markdown": "# Wait on earlier work\n",
        "issues": deepcopy(list(issues)), "edits": deepcopy(list(edits)),
    }


def publish(case, capsys, value, *options):
    case.path.write_text(json.dumps(value), encoding="utf-8")
    capsys.readouterr()
    code = cli_exit(case.main, ["order", "publish", str(case.path),
                                "--repo-root", str(case.root), *options])
    captured = capsys.readouterr()
    assert case.gh == [], "No GitHub process may run in these acceptance tests"
    return code, captured.out + captured.err


def created(case, title):
    found = [issue for issue in case.tracker.list_issues() if issue.title == title]
    assert len(found) == 1, f"Publication must create exactly one issue titled {title!r}"
    return found[0]


def blockers(case, number):
    return sorted(dep.number for dep in case.tracker.blocked_by(number))
