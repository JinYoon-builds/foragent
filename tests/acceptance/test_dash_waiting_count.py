"""Acceptance test for dash-waiting-count. Locked by Foragent; do not edit.

UX researcher r1001 (2026-11-07 03:11Z): at the same second `fa gate status` said 16
issues wait (queued 16, plus 8 waiting for a person and 3 blocked) while the dashboard
hero box said '30개는 순서를 기다려요'. The hero box counted held and orchestrator- or
person-waiting issues as waiting their turn.

- With issues in the queue, held issues and issues waiting for a person present, the
  dashboard hero box's 'waiting its turn' number equals the queued count of
  `fa gate status --json` taken from the same state.

The check reads the printed `fa gate watch --once` screen and takes the number from the
hero box's 'waiting its turn' sentence (the existing lane_bubble_queued copy) only.
"""

import json
import re

import pytest

TITLES = {"api": "Quokka", "web": "Pelican"}
WIDTHS = (80, 120, 160)
QUEUED = {2: "api", 3: "web", 4: "web"}


def _case(git_history, tmp_path, monkeypatch, language, *, queued=QUEUED):
    """One running issue, queued issues, held issues and issues waiting for a person.

    - #1 runs in api (from the shared fixture).
    - queued: approved, nothing holds them.
    - #5, #6: held by the orchestrator (blocked label).
    - #7: parked by the orchestrator (`fa orch park`), held without a person.
    - #8: parked for a person's decision (`fa orch park --for-person`).
    - #9, #10: wait for a person's answer (needs-human).
    """
    from foragent.scheduler import tick
    from test_orch_review_handoff import bind_cli, make_case, tick_context

    lanes = {slug: {"paths": [f"{slug}/**"], "title": title, "emoji": "🧩"}
             for slug, title in TITLES.items()}
    case = make_case(git_history, tmp_path, monkeypatch, lanes=lanes, language=language)
    main = bind_cli(case, monkeypatch)
    case.tracker.add_labels(1, ["lane:api"])
    case.store.update_run(1, lane="api", phase="implementing")
    for number, lane in queued.items():
        case.tracker.add_issue(number, f"Queued {number}", labels=["approved", f"lane:{lane}"])
    case.tracker.add_issue(5, "Held five", labels=["approved", "blocked", "lane:api"])
    case.tracker.add_issue(6, "Held six", labels=["approved", "blocked", "lane:web"])
    case.tracker.add_issue(7, "Parked seven", labels=["approved", "lane:api"])
    case.tracker.add_issue(8, "Parked for a person", labels=["approved", "lane:web"])
    case.tracker.add_issue(9, "Asks nine", labels=["approved", "needs-human", "lane:api"])
    case.tracker.add_issue(10, "Asks ten", labels=["approved", "needs-human", "lane:web"])
    tick(tick_context(case, start_runs=False))
    assert main(["orch", "park", "7", "--why", "Wait for the design", "--repo-root", str(case.root)]) == 0
    assert main(["orch", "park", "8", "--why", "Jin decides the scope", "--for-person",
                 "--repo-root", str(case.root)]) == 0
    tick(tick_context(case, start_runs=False))
    return case, main


def _gate_summary(main, capsys, case):
    capsys.readouterr()
    assert main(["gate", "status", "--json", "--repo-root", str(case.root)]) == 0
    return json.loads(capsys.readouterr().out)["summary"]


def _plain(text):
    return re.sub(r"\x1b\[[0-9;?]*[ -/]*[@-~]", "", text)


def _screens(main, case, capsys):
    screens = {}
    for width in WIDTHS:
        capsys.readouterr()
        with pytest.MonkeyPatch.context() as patch:
            patch.setenv("COLUMNS", str(width))
            assert main(["gate", "watch", "--once", "--repo-root", str(case.root)]) == 0
        screens[f"COLUMNS={width}"] = _plain(capsys.readouterr().out)
    return screens


def _sentence_pattern(language):
    """The hero box's 'waiting its turn' sentence, built from the existing copy."""
    from foragent import i18n

    patterns = []
    for key, count in (("lane_bubble_queued", "{count}"), ("lane_bubble_queued_one", None)):
        template = i18n.MESSAGES[language][key]
        if count is None:
            patterns.append((re.escape(template), 1))
        else:
            head, tail = template.split("{count}")
            patterns.append((re.escape(head) + r"(\d+)" + re.escape(tail), None))
    return patterns


def _waiting_numbers(screen, language):
    """Every 'N wait their turn' number the screen states (box borders and wrapping removed)."""
    flat = re.sub(r"[│|]", " ", screen)
    flat = re.sub(r"\s+", " ", flat)
    found = []
    for pattern, fixed in _sentence_pattern(language):
        pattern = pattern.replace(r"\ ", r"\s*")
        for match in re.finditer(pattern, flat):
            found.append(fixed if fixed is not None else int(match.group(1)))
    return found


@pytest.mark.parametrize("language", ["en", "ko"])
def test_hero_waiting_count_equals_gate_status_queued(git_history, tmp_path, monkeypatch, capsys, language):
    case, main = _case(git_history, tmp_path, monkeypatch, language)
    summary = _gate_summary(main, capsys, case)
    # Fixture guard: queued work, held work and work waiting for a person are all present.
    assert summary["queued"] == len(QUEUED), summary
    assert summary["blocked"] >= 2, summary
    assert summary["waiting_human"] >= 3, summary
    for name, screen in _screens(main, case, capsys).items():
        numbers = _waiting_numbers(screen, language)
        assert numbers, f"{name}: the hero box must state how many wait their turn:\n{screen}"
        assert numbers == [summary["queued"]], (
            f"{name}: the hero box's 'waiting its turn' number must be fa gate status's queued count "
            f"{summary['queued']} (held: {summary['blocked']}, waiting for a person: "
            f"{summary['waiting_human']} are not counted), found {numbers}:\n{screen}")


@pytest.mark.parametrize("language", ["en", "ko"])
def test_hero_waiting_count_follows_the_queue_not_the_holds(git_history, tmp_path, monkeypatch, capsys, language):
    """A single queued issue beside many held ones reads as one waiting its turn."""
    case, main = _case(git_history, tmp_path, monkeypatch, language, queued={2: "api"})
    summary = _gate_summary(main, capsys, case)
    assert summary["queued"] == 1, summary
    for name, screen in _screens(main, case, capsys).items():
        numbers = _waiting_numbers(screen, language)
        assert numbers == [1], (
            f"{name}: one issue waits its turn as fa gate status says; held and person-waiting "
            f"issues keep their own lines, found {numbers}:\n{screen}")
