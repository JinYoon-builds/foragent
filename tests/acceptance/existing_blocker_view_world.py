"""Shared world for the view-shows-existing-blocker acceptance tests (Jin r1301).

One gate world, offline, through public entry points only:

- #1 runs in the api lane (shared make_case fixture).
- #40 is an existing open issue published before the order: no approval, no lane.
- #39 is approved and ready in the web lane with no blocker; it is listed first in web.
- #41 is approved and ready in the web lane and blocked by an existing open issue
  (#40 by default, or the running #1), recorded as a GitHub dependency exactly as
  publication records it (FakeTracker.add_blocked_by).

A gate tick that starts nothing records the issues locally; the dashboard and
``fa brief`` then read only local state.
"""

from __future__ import annotations

import re

LANES = {"api": {"paths": ["api/**"], "title": "Quokka", "emoji": "🧩"},
         "web": {"paths": ["web/**"], "title": "Pelican", "emoji": "🧩"}}

WAITER = 41
EXISTING = 40
UNBLOCKED = 39


def waiting_for(number: int, language: str) -> re.Pattern[str]:
    """Words that say "waiting for #number", in either language, and not some other number."""
    issue = rf"#{number}(?!\d)"
    if language == "ko":
        # "#40 기다리는 중", "#40을 기다려요", "#40 머지를 기다려요", "#40이 끝나길 기다려요" ...
        return re.compile(rf"{issue}[^·│|\n#]{{0,12}}기다")
    return re.compile(rf"\bwait(?:ing|s)?\s+(?:for|on)\s+{issue}", re.IGNORECASE)


def any_waiting_for(language: str) -> re.Pattern[str]:
    if language == "ko":
        return re.compile(r"#\d+[^·│|\n#]{0,12}기다")
    return re.compile(r"\bwait(?:ing|s)?\s+(?:for|on)\s+#\d+", re.IGNORECASE)


def build(git_history, tmp_path, monkeypatch, language, *, blocker=EXISTING, with_unblocked=True):
    from foragent.scheduler import tick
    from test_orch_review_handoff import bind_cli, make_case, tick_context

    case = make_case(git_history, tmp_path, monkeypatch, lanes=LANES, language=language)
    main = bind_cli(case, monkeypatch)
    case.tracker.add_labels(1, ["lane:api"])
    case.store.update_run(1, lane="api", phase="implementing")
    case.tracker.add_issue(EXISTING, "Existing schema work", labels=[])
    if with_unblocked:
        case.tracker.add_issue(UNBLOCKED, "Free to go", labels=["approved", "ready", "lane:web"])
    case.tracker.add_issue(WAITER, "Waits on existing", labels=["approved", "ready", "lane:web"])
    case.tracker.add_blocked_by(WAITER, blocker)
    # The gate pass records the issues and their dependencies locally; it starts nothing here.
    tick(tick_context(case, start_runs=False))
    return case, main


def offline_tracker(case, monkeypatch):
    """Every tracker method raises from now on: a redraw must not reach GitHub."""
    calls: list[str] = []

    def refuse(name):
        def call(*args, **kwargs):
            calls.append(name)
            raise AssertionError(f"the dashboard redraw called GitHub: tracker.{name}")
        return call

    for name in dir(type(case.tracker)):
        if name.startswith("_") or not callable(getattr(case.tracker, name, None)):
            continue
        monkeypatch.setattr(case.tracker, name, refuse(name))
    return calls


def dashboard_opened(main, case, capsys, monkeypatch, number, *, width=220):
    """`fa gate watch --once`, then the same dashboard redrawn before and after a click on
    acorn #number, with GitHub unreachable for the click and the redraws.

    Returns (unclicked redraw, opened frame). ``number`` may be a tuple: then one opened
    frame per number (each a fresh click)."""
    import pytest
    from foragent import watch
    from test_acorn_interact import hit_positions
    from test_squirrel_running import plain_frame

    seen, real = {}, watch.render_frame

    def capture(snapshot, config, root, **kwargs):
        seen.update(snapshot=snapshot, config=config, root=root)
        return real(snapshot, config, root, **kwargs)

    capsys.readouterr()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(watch, "render_frame", capture)
        assert main(["gate", "watch", "--once", "--repo-root", str(case.root)]) == 0
    printed = plain_frame(capsys.readouterr().out)
    assert seen, "the dashboard draws through render_frame"
    calls = offline_tracker(case, monkeypatch)
    frames, before = [], None
    for one in number if isinstance(number, tuple) else (number,):
        state, layout = {"store": case.store}, {}
        draw = lambda: real(seen["snapshot"], seen["config"], seen["root"], width=width, state=state,
                            layout=layout, now=case.clock[0])
        drawn = draw()
        before = before if before is not None else plain_frame(drawn)
        hits = hit_positions(layout, drawn, width)
        assert ("acorn", one) in hits, f"the dashboard keeps a clickable acorn for #{one}"
        watch.handle_mouse("left", *hits[("acorn", one)], layout, case.store, seen["config"], state=state)
        frames.append(plain_frame(draw()))
    assert calls == [], f"the click and redraw read GitHub: {calls}"
    assert printed.strip(), "fa gate watch --once prints the dashboard"
    return before, (frames if isinstance(number, tuple) else frames[0])


def brief_lines(main, case, capsys):
    capsys.readouterr()
    assert main(["brief", "--repo-root", str(case.root)]) == 0
    return capsys.readouterr().out.splitlines()


def issue_line(lines, number):
    """The brief's item lines whose own issue (the first #N on a "- " line) is #number."""
    found = []
    for line in lines:
        match = re.match(r"^\s*-\s[^#]*#(\d+)", line)
        if match and int(match.group(1)) == number:
            found.append(line)
    return found
