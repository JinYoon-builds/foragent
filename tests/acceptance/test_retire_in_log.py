"""Acceptance (retire-in-log): a retired main write stays readable in `fa orch log`.

PM q93 (UX r1259, 2026-11-08): request 106 was retired with a reason, but once
a later request landed no read-only command showed it. After this issue

    fa orch main-write retire 106 --why TEXT --by WHO

accepts the sequence number `fa gate status` prints, and the retire is an
orchestrator intervention that `fa orch log` prints: the request's sequence
number and title, the reason, who retired it and when. It stays there after a
later request lands.

Public entry points: the producer `main_write_requests.capture`, the owner pass
`merge_owner.tick`, and the CLI `foragent.cli.main` (`fa orch main-write
retire`, `fa orch log`). Real Git on a local bare origin through the shared
offline transport (fixtures of tests/acceptance/test_one_commit_per_pass.py and
the CLI binding of tests/acceptance/test_main_write_retire.py, both locked and
read-only). No network, model or signal.

Break-it: a retire that refuses the sequence number fails the exit-code and
status checks; a retire that writes no log event, or drops the title, reason,
who or time from it, fails the log-line check; a doubled event fails the
"exactly one line" check; a retire that retires another request fails the
status checks.
"""

import json
import re
from datetime import UTC, datetime

import pytest

from foragent import main_write_requests as writes
from test_main_write_retire import bind_cli, blocked_queue, row
from test_one_commit_per_pass import (  # noqa: F401  (fixtures)
    MEMORY, case, contents, owner, owner_pass, pushes, tip)
from test_main_write_retire import LATER
from test_order import offline_publication  # noqa: F401  (fixture)

SEQUENCE = 106
WHY = "Superseded locked tests; the lock removal must not land (q93)"
WHO = "jin-pm-q93"
STAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[+-]\d{2}:\d{2}|Z)?")


def seed_earlier_requests(o, last=SEQUENCE - 1):
    """Earlier queue history, so the next request gets sequence 106 as on 11-08.

    One finished (retired by the owner, no person) request with sequence 105 from
    another checkout: capture, drain and status skip it.
    """
    directory = o.store.root / "main-writes"
    directory.mkdir(parents=True, exist_ok=True)
    filler = {"id": "0" * 32, "sequence": last, "target": o.ctx.config.base_branch,
              "title": "Earlier write", "requester": "lane elsewhere",
              "payload": {"kind": "files", "files": {}, "source": "/elsewhere"},
              "status": "retired", "reason": "Superseded by later snapshot (setup)"}
    (directory / f"{filler['id']}.json").write_text(json.dumps(filler, indent=2) + "\n")


def parse_stamp(text):
    value = text.replace("Z", "+00:00")
    stamp = datetime.fromisoformat(value)
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=UTC)


@pytest.mark.parametrize("by", ["sequence", "id"])
def test_retired_request_106_is_shown_in_orch_log_after_a_later_request_lands(owner, monkeypatch, capsys, by):
    o = owner
    fa = bind_cli(o, monkeypatch)
    seed_earlier_requests(o)
    stuck, later = blocked_queue(o)
    assert stuck["sequence"] == SEQUENCE, f"setup: the blocked request must be request {SEQUENCE}: {stuck}"
    title = stuck["title"]
    name = str(SEQUENCE) if by == "sequence" else stuck["id"]

    started = datetime.now(UTC)
    code, out = fa(capsys, "orch", "main-write", "retire", name, "--why", WHY, "--by", WHO)
    finished = datetime.now(UTC)

    assert code == 0, f"`retire {name}` (sequence number or id) must retire request {SEQUENCE}: {out}"
    retired = row(o, stuck["id"])
    assert retired["status"] == "retired", f"request {SEQUENCE} must be retired: {retired}"
    assert row(o, later["id"])["status"] == "pending", "only request 106 is retired"

    # A later request lands; the retire then no longer shows in gate status (by design).
    before = tip(o)
    owner_pass(o)
    assert row(o, later["id"])["status"] == "done", "setup: the later request must land"
    assert tip(o) != before and contents(o, LATER) is not None
    assert stuck["id"] not in {r["id"] for r in writes.summary(o.store)}, \
        "gate status keeps showing current work only"

    code, out = fa(capsys, "orch", "log")
    assert code == 0, out
    lines = [line for line in out.splitlines() if WHY in line]
    assert len(lines) == 1, f"`fa orch log` must show the retire exactly once with its reason: {out!r}"
    line = lines[0]
    assert title in line, f"the log line must name the retired request's title {title!r}: {line!r}"
    assert re.search(rf"(?<!\d){SEQUENCE}(?!\d)", line), f"the log line must name request {SEQUENCE}: {line!r}"
    assert WHO in line, f"the log line must name who retired it ({WHO}): {line!r}"
    stamps = [parse_stamp(match) for match in STAMP.findall(line)]
    fixed = o.ctx.now()
    assert any(started.replace(microsecond=0) <= stamp <= finished or stamp == fixed for stamp in stamps), (
        f"the log line must say when it was retired (between {started} and {finished}): {line!r}")
