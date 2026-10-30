"""Offline boundaries shared by the team-v1 acceptance goals."""
from copy import copy
from dataclasses import asdict
import json

from foragent.state import StateStore
from test_orch_review_handoff import make_case, tick_context


def person(tracker, monkeypatch, login):
    # Public tracker contract supplied by the identity issue. This is fake I/O,
    # not a replacement for ownership parsing, admission, or action policy.
    monkeypatch.setattr(tracker, "authenticated_login", lambda: login, raising=False)
    return tracker


def legacy(config, login="alice"):
    # Let old main reach observable entry-point behavior before the new config
    # field exists; the implementation must add/validate this real config field.
    object.__setattr__(config, "legacy_owner", login)
    return config


def factory(case, directory, login, monkeypatch):
    tracker = person(copy(case.tracker), monkeypatch, login)
    store = StateStore(directory, now=lambda: case.clock[0])
    ctx = tick_context(case)
    ctx.tracker, ctx.store = tracker, store
    ctx.start_runs = True
    starts = []
    ctx.launcher = lambda n: starts.append(n) or 7000 + n
    ctx.baseline_launcher = lambda *a, **kw: None
    return ctx, starts


def snapshot(case):
    return {
        "issues": [asdict(v) for _, v in sorted(case.tracker.issues.items())],
        "comments": {n: [asdict(v) for v in case.tracker.list_comments(n)]
                     for n in case.tracker.issues},
        "prs": [asdict(v) for v in case.tracker.created_prs],
        "merges": list(case.tracker.merged_prs),
        "state": {p.relative_to(case.store.root).as_posix(): p.read_bytes()
                  for p in case.store.root.rglob("*") if p.is_file()},
    }


def rows(store):
    return {r["number"]: r for r in store.local_issues()}


def invoke(main, args, capsys):
    try:
        code = main(args)
    except SystemExit as exc:
        code = exc.code
    out = capsys.readouterr()
    return code, out.out + out.err
