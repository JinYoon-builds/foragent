"""Acceptance: alerts on the default route reach the planning session; no unread outbox (#1650).

The default notify setting, and the older `claude-push` value still found in people's
files, hand an alert to the planning (PM) session, which relays it to the person. No
file that nothing reads is written for it. Observed only through `fa notify`, the
planning session's prompt hook and the files left on disk.
"""

from pathlib import Path

import pytest
import yaml

from foragent import cli
from test_notify_personal_decisions import planning_notify_case  # noqa: F401
from test_presence_order_stall import prompt
from test_skills_autosync import cli_exit, isolated_home  # noqa: F401
from test_fa_report_since import report_case  # noqa: F401
from test_presence_session_start import session_case  # noqa: F401


def personal_setting(**values):
    """Write the person's own notify settings; None removes the key (the default)."""
    path = Path.home() / ".config/foragent/config.yml"
    current = yaml.safe_load(path.read_text()) or {}
    for key, value in values.items():
        current.pop(key, None)
        if value is not None:
            current[key] = value
    path.write_text(yaml.safe_dump(current))


def outbox_files(case, tmp_path):
    """Every file or folder named like an outbox in the isolated home, state and project."""
    found = set()
    for base in {Path.home(), case.root, case.store.root, tmp_path}:
        if base.exists():
            found |= {path for path in base.rglob("*") if "outbox" in path.name.lower()}
    return sorted(found)


@pytest.mark.parametrize("setting", [None, "claude-push"], ids=["default", "older-claude-push"])
def test_default_route_alerts_reach_planning_session_without_outbox(
    planning_notify_case, monkeypatch, tmp_path, setting,
):
    case = planning_notify_case
    personal_setting(notify=setting, notify_filter="all")
    sent = {
        "question": ("Decision Ψ17 waiting", "https://example.test/pull/17"),
        "blocked": ("Status Ж23 stuck", None),
    }
    for kind, (line, link) in sent.items():
        argv = ["notify", "--kind", kind, "--title", f"Title {kind}", "--line", line,
                "--repo-root", str(case.root)]
        if link:
            argv += ["--link", link]
        assert cli_exit(cli.main, argv) == 0, f"The {kind} alert must be accepted on the default route"
    context = prompt(case, monkeypatch)
    for kind, (line, link) in sent.items():
        assert context.count(line) == 1, (
            f"The planning session must see the {kind} alert exactly once; got:\n{context}")
    assert "https://example.test/pull/17" in context, "The link the person needs must travel with the alert"
    assert outbox_files(case, tmp_path) == [], "No unread outbox may be written"


def test_decision_alert_under_decisions_filter_reaches_planning_session_without_outbox(
    planning_notify_case, monkeypatch, tmp_path,
):
    case = planning_notify_case
    personal_setting(notify=None, notify_filter="decisions")
    assert "Merge Ω58 approval" not in prompt(case, monkeypatch)
    assert cli_exit(cli.main, ["notify", "--kind", "merge", "--title", "Merge", "--line",
                               "Merge Ω58 approval", "--repo-root", str(case.root)]) == 0
    context = prompt(case, monkeypatch)
    assert context.count("Merge Ω58 approval") == 1, (
        f"A decision alert must reach the planning session once; got:\n{context}")
    assert outbox_files(case, tmp_path) == [], "No unread outbox may be written"
