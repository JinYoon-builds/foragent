"""Acceptance test for acorn-interact. Locked by Foragent; do not edit."""

from dataclasses import replace
from io import StringIO

from test_local_truth import record_gh, snapshot_case
from test_skills_autosync import isolated_home
from test_squirrel_running import display_cells, issue_row, plain_frame, running_snapshot, view_offline


def interaction_snapshot(count=7):
    """Contract: render_frame fills caller-owned layout; handlers mutate state.

    State's issue is the open box number (or None), and scroll is a zero-based
    content offset. Mouse coordinates and hit_test are zero-based screen cells.
    """
    snapshot, config = running_snapshot()
    row = snapshot.status["lanes"]["work"]
    row.update(issue=3, title="Current task", running=[{"number": 3, "title": "Current task"}],
               queued=[{"number": n, "title": f"Task {n}"} for n in range(4, count + 1)])
    status = {**snapshot.status, "lanes": {"work": row},
              "summary": {"working": 1, "blocked": 0, "waiting_human": 0, "queued": count - 3},
              "active_runs": [{"number": 3, "lane": "work", "attempts": 1}]}
    return (replace(snapshot, status=status, done={"work": snapshot.done["work"]}, completed=2, total=count),
            replace(config, lanes={"work": config.lanes["work"]}))


def hit_positions(layout, frame, width):
    """Discover hit boxes from the actual public renderer, not a made-up layout."""
    from foragent.watch import hit_test

    hits = {}
    for y, _ in enumerate(plain_frame(frame).splitlines()):
        for x in range(width):
            target = hit_test(layout, x, y)
            if target is not None:
                hits.setdefault(target, (x, y))
    return hits


def interaction_api(*names):
    from foragent import watch

    for name in names:
        assert callable(getattr(watch, name, None)), f"the public lane-view interaction API must expose {name}"
    return tuple(getattr(watch, name) for name in names)


def row_hit_positions(layout, frame, width, numbers):
    from foragent.watch import hit_test
    from test_lane_m2_contract import acorn_row
    hits=hit_positions(layout,frame,width)
    positions={}
    for number in numbers:
        acorn_row(frame,number)
        assert ('acorn',number) in hits, 'each visible issue acorn has its own real hit region'
        x,y=hits[('acorn',number)]
        assert hit_test(layout,x,y)==('acorn',number)
        positions[number]=(x,y)
    return positions


def test_acorn_interact_issue_box_reads_all_details_from_local_records_without_gh(
    git_history, tmp_path, monkeypatch,
):
    issue_box, = interaction_api("issue_box")

    case = snapshot_case(git_history, tmp_path, monkeypatch)
    # The local body is the approved fixture body. A remote edit must stay invisible.
    case.tracker.update_issue(1, title="Remote title", body="Remote goal")
    calls = record_gh(monkeypatch)
    lines = issue_box(1, case.store, case.config)
    assert isinstance(lines, list) and all(isinstance(line, str) for line in lines)
    box = plain_frame("\n".join(lines))
    for value in ("#1", "Implement feature", "Api", "Implement the feature.",
                  "test -f feature.txt", "https://github.com/owner/repo/issues/1"):
        assert value in box, (value, box)
    assert "writing code" in box.lower(), "the English issue box must use the current implementing phase word"
    assert "Remote title" not in box and "Remote goal" not in box
    assert calls == []


def test_acorn_interact_collapsed_neighbours_expand_all_and_more_hits_are_real(tmp_path):
    from foragent.state import StateStore
    from test_lane_m2_contract import acorn_row
    handle_key,handle_mouse,hit_test,render_frame=interaction_api('handle_key','handle_mouse','hit_test','render_frame')
    snapshot,config=interaction_snapshot(count=52)
    store,state,layout=StateStore(tmp_path/'state'),{},{}
    frame=render_frame(snapshot,config,tmp_path,width=80,state=state,layout=layout)
    hits=hit_positions(layout,frame,80)
    assert ('acorn',3) in hits
    visible={target[1] for target in hits if target[0]=='acorn' and 4<=target[1]<=52}
    assert len(visible)>1, 'replacement: fill available next rows before truncating the queue'
    assert f'…{49-len(visible)}' in plain_frame(frame)
    assert hit_test(layout,-1,-1) is None and hit_test(layout,80,0) is None
    for n in visible: acorn_row(frame,n)


def test_acorn_interact_wheel_and_j_k_scroll_without_clicks_and_show_position(tmp_path):
    from foragent.state import StateStore
    handle_key,handle_mouse,render_frame=interaction_api('handle_key','handle_mouse','render_frame')
    snapshot,config=interaction_snapshot(count=52)
    store,state,layout=StateStore(tmp_path/'state'),{},{}
    frame=render_frame(snapshot,config,tmp_path,width=56,height=8,state=state,layout=layout)
    before=state.get('scroll',0)
    handle_key('j',store,config,state=state)
    assert state['scroll']>before
    handle_key('k',store,config,state=state)
    assert state['scroll']==before
    for event in ('wheel-down','wheel-up'):
        state['scroll']=1
        handle_mouse(event,0,0,layout,store,config,state=state)
        assert state['scroll']>1 if event=='wheel-down' else state['scroll']<1
        assert state.get('issue') is None
    assert 's ' in plain_frame(frame).splitlines()[-1] and 'm ' in plain_frame(frame).splitlines()[-1]
    assert 'Claude' in plain_frame(frame).splitlines()[-2] and 'Codex' in plain_frame(frame).splitlines()[-2]


def test_acorn_interact_left_click_toggles_box_and_x_closes_it(
    git_history, tmp_path, monkeypatch,
):
    from test_lane_m2_contract import draw,pick,bubble
    handle_key,handle_mouse,render_frame=interaction_api('handle_key','handle_mouse','render_frame')
    case=snapshot_case(git_history,tmp_path,monkeypatch)
    snapshot,config=interaction_snapshot()
    row=snapshot.status['lanes']['work']
    row.update(issue=1,title='Implement feature',running=[{'number':1,'title':'Implement feature'}])
    snapshot.status['active_runs'][0].update(number=1,phase='implementing')
    state={'store':case.store}
    calls=record_gh(monkeypatch)
    before=draw(snapshot,config,case.root,width=160,state=state)
    opened,state=pick(snapshot,config,case.root,case.store,('acorn',1),width=160,state=state)
    assert 'Implement feature' in bubble(opened) and '#1' in opened
    layout={}
    render_frame(snapshot,config,case.root,width=160,state=state,layout=layout)
    assert ('acorn',1) in hit_positions(layout,opened,160), 'explanation keeps the lane acorn visible'
    handle_key('x',case.store,config,state=state)
    assert draw(snapshot,config,case.root,width=160,state=state)==before
    assert calls==[]


def test_acorn_interact_live_numbers_use_osc8_and_live_setup_enters_alternate_screen(
    git_history, tmp_path, monkeypatch,
):
    import curses
    from foragent import watch
    osc8, terminal_setup_sequences = interaction_api("osc8", "terminal_setup_sequences")

    url = "https://github.com/owner/repo/issues/1"
    assert osc8(url, "#1") == f"\x1b]8;;{url}\x1b\\#1\x1b]8;;\x1b\\"
    sequences = "".join(terminal_setup_sequences())
    assert "\x1b[?1049h" in sequences
    case = snapshot_case(git_history, tmp_path, monkeypatch)
    from test_orch_review_handoff import bind_cli
    main = bind_cli(case, monkeypatch)
    # Capture status through the public CLI without a real tty.
    import json
    import contextlib
    status_output = StringIO()
    with contextlib.redirect_stdout(status_output):
        assert main(["gate", "status", "--json"]) == 0
    status = json.loads(status_output.getvalue())
    snapshot, config = interaction_snapshot()
    snapshot.status["lanes"]["work"].update(issue=1, title="Implement feature", running=[{"number":1,"title":"Implement feature"}])
    snapshot.status["active_runs"][0].update(number=1, phase="implementing")
    state, layout = {}, {}
    watch.handle_key("a", case.store, config, state=state)
    frame = watch.render_frame(snapshot, config, case.root, width=100, state=state, layout=layout)
    row_hit_positions(layout, frame, 100, (1,))
    linked, setup_calls, painted = [], [], []
    original_setup = terminal_setup_sequences

    def link(url, text):
        linked.append((url, text))
        return osc8(url, text)

    def setup():
        setup_calls.append(True)
        return original_setup()

    class TTY(StringIO):
        def isatty(self):
            return True

    class Screen:
        def getmaxyx(self):
            return 100, 100

        def getch(self):
            return ord("q")

        def addstr(self, *args):
            painted.append(args)

        def keypad(self, value):
            pass

        def timeout(self, value):
            pass

        def erase(self):
            pass

        def refresh(self):
            pass

    output = TTY()
    monkeypatch.setattr(watch.sys, "stdin", TTY())
    monkeypatch.setattr(watch.sys, "stdout", output)
    monkeypatch.setattr(curses, "wrapper", lambda callback, *args: callback(Screen(), *args))
    monkeypatch.setattr(curses, "has_colors", lambda: False)
    monkeypatch.setattr(curses, "curs_set", lambda value: None)
    monkeypatch.setattr(curses, "mousemask", lambda value: (value, 0))
    monkeypatch.setattr(curses, "mouseinterval", lambda value: None)
    monkeypatch.setattr(watch, "osc8", link)
    monkeypatch.setattr(watch, "terminal_setup_sequences", setup)
    watch.run(lambda: status, case.tracker, case.store, case.config, case.root, now=lambda: case.clock[0])
    assert (url, "#1") in linked, "the live renderer must use the public OSC 8 builder"
    assert painted
    terminal_output = output.getvalue() + "".join(
        value for args in painted for value in args if isinstance(value, str))
    assert osc8(url, "#1") in terminal_output, "the linked number must actually reach the terminal"
    assert setup_calls and "\x1b[?1049h" in output.getvalue()

