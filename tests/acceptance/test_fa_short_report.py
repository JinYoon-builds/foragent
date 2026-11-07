"""Acceptance tests for short-report (D127): `fa brief --report` is at most three lines.

Line 1: counts (working now, merged since the last report, decisions waiting).
Line 2: the one thing that matters most (latest orchestrator judgment, else the worst
scorecard metric, else left out). Line 3: what happens next. The full detail stays
available from the CLI, and /fa asks each decision as its own multiple-choice question.
"""

from datetime import UTC, datetime, timedelta
import json
import os
from pathlib import Path
import re
import shlex
import socket
import subprocess
import time
from types import SimpleNamespace
import unicodedata

import pytest
import yaml

from test_fa_report_since import contains_time
from test_local_truth import record_gh
from test_skills_autosync import cli_exit, isolated_home  # noqa: F401  (fixture)


NOW = datetime(2026, 11, 5, 3, tzinfo=UTC)
REPO = Path(__file__).resolve().parents[2]
UPDATE = {"latest": "9.9.9", "current": "0.1.0", "command": "uv tool upgrade foragent"}
FIRST_RUN_GREETING = (
    "      ▄██▄  ▄██▄\n"
    "  ▄▄▄▄██████████\n"
    "▄████████████████\n"
    "██████████████████\n"
    "█████████████████\n"
    " ███████████████▀\n"
    "  ▀▀███████▀███\n"
    "        ▀▀   ▀▀\n"
    "\n"
    "Hi, I'm Foragent. This folder isn't set up yet. Tell me what you want to build, "
    "and I'll set it up with you.\n"
)
WAITING_NEXT = re.compile(r"one at a time|one by one|one after (?:the )?other|one after another|"
                          r"each (?:on its own|in turn|separately)|separately|하나씩|차례로|한 개씩", re.I)
NOTHING_NEXT = re.compile(r"nothing (?:is )?(?:waiting|needs you|needs your|to decide|for you)|"
                          r"no decisions? (?:is |are )?(?:waiting|needed|open|to make)|"
                          r"none (?:is |are )?waiting|nothing waits|"
                          r"정해 주실 일(?:이|은) 없|기다리는 결정(?:이|은) 없|결정할 (?:일|것)(?:이|은) 없", re.I)
ANSI = re.compile(r"\x1b\[[0-9;]*m")


def width(line):
    """Terminal columns of one line: wide (W/F) characters take two columns."""
    plain = ANSI.sub("", line)
    return sum(0 if unicodedata.combining(char) else
               2 if unicodedata.east_asian_width(char) in ("W", "F") else 1 for char in plain)


def has_count(line, number):
    return re.search(rf"(?<![\d#.]){number}(?![\d])", line) is not None


@pytest.fixture
def short(tmp_path, monkeypatch, isolated_home, capsys):  # noqa: F811
    from foragent import cli, orch_inbox, state, statusline, update_check
    from foragent.orch_events import append_event
    from foragent.state import StateStore
    from foragent.tracker import Issue

    root = tmp_path / "project"
    root.mkdir()
    (root / ".git").mkdir()
    values = {"repo": "offline/short-report", "language": "en", "skill_libraries": [],
              "lanes": {"api": {"paths": ["api/**"]}, "decision": {"paths": ["decision/**"]}}}

    def configure(**changes):
        values.update(changes)
        (root / ".foragent.yml").write_text(yaml.safe_dump(values), encoding="utf-8")

    configure()
    clock = [NOW]

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock[0].astimezone(tz) if tz else clock[0].replace(tzinfo=None)

    for module in (cli, state, statusline):
        monkeypatch.setattr(module, "datetime", Clock)
    store = StateStore.for_repo(values["repo"], now=lambda: clock[0])
    factory = StateStore.for_repo.__func__
    monkeypatch.setattr(StateStore, "for_repo", classmethod(
        lambda cls, repo, **kwargs: factory(cls, repo, now=lambda: clock[0])))
    monkeypatch.chdir(root)
    monkeypatch.setenv("LANG", "en_US.UTF-8")
    monkeypatch.setattr(cli.star, "offer", lambda language: None)
    offer = [None]
    monkeypatch.setattr(update_check, "cached", lambda config: offer[0])

    def forbidden(*args, **kwargs):
        pytest.fail("The report must use local state only: no network, child process or sleep")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(time, "sleep", forbidden)
    gh_calls = record_gh(monkeypatch)
    issues = []

    def snapshot_issues():
        store.refresh_local_issues(list(issues), [], values["lanes"], clock[0])

    def merge(number, at, title=None):
        saved = clock[0]
        if title:
            issues.append(Issue(number, title, "", frozenset({"lane:api"}), "closed"))
            snapshot_issues()
        clock[0] = at - timedelta(minutes=10)
        store.start_run(number, branch=f"foragent/issue-{number}",
                        worktree=store.worktree_path(number), lane="api")
        clock[0] = at
        store.record_merge(number, at)
        store.finish_run(number, outcome="merged")
        clock[0] = saved

    def run(number):
        issues.append(Issue(number, f"Running work {number}", "", frozenset({"running", "lane:api"}), "open"))
        snapshot_issues()
        store.start_run(number, branch=f"foragent/issue-{number}",
                        worktree=store.worktree_path(number), lane="api")

    def release_hold(number, say):
        issues.append(Issue(number, f"Held work {number}", "", frozenset({"lane:api"}), "open"))
        snapshot_issues()
        append_event(store, NOW - timedelta(hours=3), number, "escalate", f"Hold on #{number}")
        append_event(store, NOW - timedelta(hours=1), number, "retry", f"#{number}: queued retry", say=say)

    def person(number, title):
        issues.append(Issue(number, title, "", frozenset({"needs-human", "lane:decision"}), "open"))
        snapshot_issues()

    def ask(question, options, recommend, issue):
        return orch_inbox.ask(store, question, options, recommend, issues=[issue])

    def judge(say, at=None):
        append_event(store, at or clock[0], None, "note", "Agent's current judgment", say=say)

    def invoke(*argv):
        # A documented detail command may omit --repo-root; its cwd must still
        # be this temporary project, never tests/acceptance.
        assert Path.cwd() == root
        capsys.readouterr()
        code = cli_exit(cli.main, list(argv))
        captured = capsys.readouterr()
        return code, captured.out, captured.err

    def brief(*options, at=None):
        code, out, err = invoke("brief", *options, "--repo-root", str(at or root))
        assert gh_calls == [], "fa brief --report must make zero GitHub calls"
        assert code == 0, f"fa brief {' '.join(options)} must succeed; exit={code}: {err.strip()[-300:]}"
        return out

    def report(*options, at=None):
        output = brief("--report", *options, at=at)
        return output.splitlines()

    def state_files():
        return {path: path.read_bytes() for path in store.root.rglob("*") if path.is_file()}

    def restore(saved):
        for path in list(store.root.rglob("*")):
            if path.is_file() and path not in saved:
                path.unlink()
        for path, raw in saved.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)

    yield SimpleNamespace(root=root, store=store, clock=clock, configure=configure, merge=merge,
                          run=run, release_hold=release_hold, person=person, ask=ask, judge=judge,
                          brief=brief, report=report, invoke=invoke, offer=offer, issues=issues,
                          state_files=state_files, restore=restore, gh_calls=gh_calls)
    assert gh_calls == [], "Reports must make zero GitHub calls"


SCALES = {
    # working, merged, held-and-released, orchestrator questions, needs-human issues
    "small": (3, 7, 2, 2, 3),
    "large": (4, 26, 12, 9, 6),
}


def seed(case, scale):
    working, merged, holds, questions, persons = SCALES[scale]
    seeded = SimpleNamespace(merged=[], holds=[], questions=[], persons=[], working=working,
                             decisions=questions + persons, merged_count=merged)
    for index in range(working):
        case.run(301 + index)
    for index in range(merged):
        number, title = 101 + index, f"Merged feature number {index} title"
        case.merge(number, NOW - timedelta(minutes=5 + index * 7), title=title)
        seeded.merged.append((number, title))
    for index in range(holds):
        number, say = 201 + index, f"Hold {index} on the schema is released; work goes on."
        case.release_hold(number, say)
        seeded.holds.append((number, say))
    for index in range(questions):
        text = f"Question {index}: which storage region should we use?"
        options = [f"Seoul-{index}", f"Tokyo-{index}", f"Oregon-{index}"]
        seeded.questions.append((case.ask(text, options, 2, 401 + index), text, options))
    for index in range(persons):
        number, title = 501 + index, f"Person decision {index} about pricing"
        case.person(number, title)
        seeded.persons.append((number, title))
    return seeded


def split_update(lines, case, language="en"):
    from foragent.update_check import offer_line

    if case.offer[0] is None:
        return lines, []
    update = offer_line(case.offer[0], language)
    return [line for line in lines if line.strip() != update.strip()], [line for line in lines
                                                                        if line.strip() == update.strip()]


@pytest.mark.parametrize("scale", sorted(SCALES))
@pytest.mark.parametrize("update", [False, True], ids=["no-update", "update"])
def test_report_is_at_most_three_lines_plus_the_update_line(short, scale, update):
    seed(short, scale)
    short.judge("Keep the API work running; the storage region needs your decision.")
    if update:
        short.offer[0] = UPDATE
    lines = short.report()
    body, updates = split_update(lines, short)
    assert len(body) <= 3, (f"The report is at most three lines whatever the counts ({scale}); "
                            f"got {len(body)}:\n" + "\n".join(lines))
    assert len(body) >= 2, "The report keeps at least the counts line and the next-step line"
    if update:
        assert len(updates) == 1, "An available update adds exactly one update line"
        assert lines[-1] == updates[0], "The update line comes last"
    else:
        assert updates == []


@pytest.mark.parametrize("scale", sorted(SCALES))
def test_first_line_gives_working_merged_and_waiting_decision_counts(short, scale):
    seeded = seed(short, scale)
    # Line 1 is the counts even when the orchestrator has a judgment (that goes on line 2).
    short.judge("Keep the API work running; the storage region needs your decision.")
    # An answered question no longer waits; a closed needs-human issue does not count.
    answered = short.ask("Answered already?", ["Yes", "No"], 1, 499)
    from foragent import orch_inbox
    from foragent.tracker import Issue
    orch_inbox.answer(short.store, answered["id"], "Yes")
    short.issues.append(Issue(599, "Closed decision", "", frozenset({"needs-human"}), "closed"))
    short.store.refresh_local_issues(list(short.issues), [], {"api": {"paths": ["api/**"]},
                                                              "decision": {"paths": ["decision/**"]}}, NOW)
    lines = short.report()
    first = lines[0]
    assert has_count(first, seeded.working), f"Line 1 counts work running now ({seeded.working}): {first!r}"
    assert has_count(first, seeded.merged_count), (
        f"Line 1 counts issues merged since the last report ({seeded.merged_count}): {first!r}")
    assert has_count(first, seeded.decisions), (
        f"Line 1 counts open orchestrator questions plus open needs-human issues "
        f"({seeded.decisions}): {first!r}")
    counts = [int(value) for value in re.findall(r"(?<![\d#.])\d+(?![\d])", first)]
    assert sorted(counts) == sorted([seeded.working, seeded.merged_count, seeded.decisions]), (
        f"Each of the three counts appears exactly once: {first!r}")
    assert not re.search(r"#\d", first), "Line 1 holds counts, not issue numbers"


def test_counts_restart_after_each_report_and_the_report_time_is_recorded_each_run(short):
    seed(short, "small")
    short.judge("Keep the API work running.")
    before = short.state_files()
    short.report()
    after = short.state_files()
    changed = [path for path in after if before.get(path) != after[path]]
    assert any(contains_time(after[path], NOW) for path in changed), "Each report run records the report time"
    short.clock[0] = NOW + timedelta(minutes=10)
    short.merge(190, NOW + timedelta(minutes=5))
    lines = short.report()
    first = lines[0]
    assert has_count(first, 1), f"Only the one merge after the last report is counted: {first!r}"
    assert not has_count(first, 7) and not has_count(first, 8), (
        f"Merges reported before are not counted again: {first!r}")
    short.clock[0] = NOW + timedelta(minutes=20)
    again = short.report()
    assert has_count(again[0], 0), f"Right after a report, zero merges are new: {again[0]!r}"


def test_second_line_is_the_latest_orchestrator_judgment(short):
    seed(short, "small")
    old = "Older judgment: wait for the schema decision first."
    latest = "Keep the API work running; the storage region needs your decision."
    short.judge(old, at=NOW - timedelta(minutes=30))
    short.judge(latest, at=NOW)
    lines = short.report()
    assert len(lines) == 3, "Counts, the one thing that matters most, and what happens next"
    assert latest in lines[1], f"Line 2 is the orchestrator's latest judgment: {lines[1]!r}"
    assert sum(latest in line for line in lines) == 1, "The judgment appears once"
    assert not any(old in line for line in lines), "An older judgment is not shown"


def _seed_scorecard_regression(short):
    """Six earlier days of 10-minute merges and one 36-minute merge today."""
    from foragent.issue import body_hash

    store, clock = short.store, short.clock
    runs = [(100 + day, NOW - timedelta(days=day, hours=6), 10) for day in range(1, 7)]
    runs.append((110, NOW - timedelta(hours=2), 36))
    for number, finished, minutes in runs:
        clock[0] = finished - timedelta(minutes=minutes)
        body = f"Scorecard fixture #{number}"
        store.save_approval(number, body=body, body_sha=body_hash(body), acceptance_blobs={},
                            approved_by="human", approved_at=clock[0].isoformat())
        store.start_run(number, branch=f"foragent/issue-{number}", worktree=store.worktree_path(number))
        clock[0] = finished
        store.update_run(number, last_progress_at=finished.isoformat())
        store.finish_run(number, outcome="merged")
    clock[0] = NOW


def test_second_line_falls_back_to_the_worst_moved_scorecard_metric(short):
    _seed_scorecard_regression(short)
    expected = json.loads(short.brief("--json"))["scorecard_line"]
    assert expected, "Fixture precondition: the scorecard names a metric that got worse"
    lines = short.report()
    assert len(lines) == 3, "Counts, the worst-moved metric, and what happens next:\n" + "\n".join(lines)
    assert lines[1].strip() == expected.strip(), (
        f"Without a judgment, line 2 is the worst-moved scorecard metric {expected!r}: {lines[1]!r}")
    assert sum(expected.strip() in line for line in lines) == 1


def test_second_line_is_left_out_without_judgment_or_worse_metric(short):
    lines = short.report()
    assert len(lines) == 2, ("With no judgment and no worse metric, the report is the counts line "
                             "and the next-step line only:\n" + "\n".join(lines))
    assert NOTHING_NEXT.search(lines[1]), f"With nothing waiting, the last line says so: {lines[1]!r}"


def test_third_line_says_waiting_decisions_will_be_asked_one_at_a_time(short):
    seed(short, "small")
    short.judge("Keep the API work running.")
    lines = short.report()
    assert len(lines) == 3
    assert WAITING_NEXT.search(lines[2]), (
        f"With decisions waiting, line 3 says they will be asked one at a time: {lines[2]!r}")
    assert not NOTHING_NEXT.search(lines[2]), "Line 3 must not also say that nothing is waiting"


@pytest.mark.parametrize("only", ["question", "person-issue"])
def test_a_single_kind_of_waiting_decision_still_counts_as_waiting(short, only):
    if only == "question":
        short.ask("Which storage region should we use?", ["Seoul", "Tokyo"], 1, 401)
    else:
        short.person(501, "Pick the price")
    short.judge("Keep going.")
    lines = short.report()
    assert has_count(lines[0], 1), f"One decision waits: {lines[0]!r}"
    assert WAITING_NEXT.search(lines[-1]), f"The next step says it will be asked: {lines[-1]!r}"


def test_third_line_says_nothing_is_waiting_when_no_decision_waits(short):
    short.run(301)
    short.merge(101, NOW - timedelta(hours=1))
    question = short.ask("Answered already?", ["Yes", "No"], 1, 401)
    from foragent import orch_inbox
    orch_inbox.answer(short.store, question["id"], "Yes")
    short.judge("Keep the API work running; nothing needs your attention.")
    lines = short.report()
    assert len(lines) == 3
    assert has_count(lines[0], 0), f"Zero decisions wait: {lines[0]!r}"
    assert NOTHING_NEXT.search(lines[2]), f"With nothing waiting, line 3 says so: {lines[2]!r}"
    assert not WAITING_NEXT.search(lines[2]), "Line 3 must not promise questions when none wait"


@pytest.mark.parametrize("scale", sorted(SCALES))
def test_no_line_lists_merges_holds_questions_or_options(short, scale):
    seeded = seed(short, scale)
    short.judge("Keep the API work running; decisions wait for you.")
    output = "\n".join(short.report())
    for number, title in seeded.merged:
        assert f"#{number}" not in output and title not in output, f"Merged issue #{number} is not listed"
    for number, say in seeded.holds:
        assert f"#{number}" not in output and say not in output, f"Hold release #{number} is not listed"
    for _question, text, options in seeded.questions:
        assert text not in output, "Question text is not in the report"
        for option in options:
            assert option not in output, f"Question option {option!r} is not in the report"
    for number, title in seeded.persons:
        assert f"#{number}" not in output and title not in output, (
            f"Needs-human issue #{number} is counted, not listed")
    assert not re.search(r"recommend", output, re.I), "No options, so no recommended option"


@pytest.mark.parametrize("language", ["en", "ko"])
def test_every_line_fits_one_100_column_row_and_a_long_note_is_cut(short, language):
    short.configure(language=language)
    seed(short, "large")
    if language == "en":
        note = ("Keep the API work running while the storage region decision waits, because the "
                "session store, the billing export and the audit trail all depend on it, and the "
                "review lane has already asked twice about the retention window for old sessions, "
                "so please answer the region question first and then the pricing one after it.")
    else:
        note = ("저장소 지역 결정이 기다리는 동안 API 작업은 계속 돌려요. 세션 저장소와 결제 내보내기와 "
                "감사 기록이 모두 이 결정에 달려 있고, 리뷰 레인이 오래된 세션의 보관 기간을 벌써 두 번 "
                "물었어요. 그래서 지역 질문에 먼저 답해 주시고 그다음에 가격 질문에 답해 주세요.")
    short.judge(note)
    short.offer[0] = UPDATE
    lines = short.report()
    body, _updates = split_update(lines, short, language)
    assert len(body) <= 3, "A long note is cut, not wrapped onto more lines:\n" + "\n".join(lines)
    for line in lines:
        assert width(line) <= 100, f"Line is {width(line)} columns, more than one 100-column row: {line!r}"
    assert any(note[:20] in line for line in body), "The note is cut from its start, not dropped"
    assert not any(note in line for line in body), "A note longer than one row is cut"


def _detail_commands(short):
    """Existing JSON detail, or a full/detail/long view documented by `fa brief --help`."""
    code, help_text, err = short.invoke("brief", "--help")
    assert code == 0, err
    commands = [["brief", "--report", "--json"]]
    commands += [["orch", "questions"], ["orch", "questions", "--json"]]
    for line in help_text.splitlines():
        if not re.search(r"full|detail|long", line, re.I):
            continue
        # Accept a documented command (e.g. `fa report detail`) as well as a
        # boolean option (e.g. --full). The lane chooses the CLI spelling.
        for match in re.finditer(r"\bfa\s+([a-z][\w-]*(?:\s+[a-z][\w-]*)*(?:\s+--[a-z][\w-]*)*)", line):
            words = shlex.split(match[1])
            # A prose suffix after a command is not part of its argv. Trying
            # prefixes also accepts help such as 'fa report for full detail'.
            commands += [words[:end] for end in range(1, len(words) + 1)]
        for flag, metavar in re.findall(r"(?<![\w-])(--[a-z][\w-]*)(?:[ =]([A-Z][A-Z_]*)(?=\s|$))?", line):
            if not metavar and flag not in {"--help", "--json", "--report", "--repo-root"}:
                commands += [["brief", "--report", flag], ["brief", flag]]
        subcommand = re.match(r"\s*(full|detail(?:s)?|long)\s{2,}", line, re.I)
        if subcommand:
            commands.append(["brief", subcommand[1]])
    return [[*command, "--repo-root", str(short.root)] for command in commands]


def test_full_detail_stays_available_from_the_cli(short):
    seeded = seed(short, "small")
    short.judge("Keep the API work running; decisions wait for you.")
    saved = short.state_files()
    short.report()
    detail = []
    commands = _detail_commands(short)
    for command in commands:
        short.restore(saved)  # Each detail view sees the same window as the short report did.
        code, out, _err = short.invoke(*command)
        if code == 0:
            detail.append(out)
    shown = "\n".join(detail)
    missing = [f"merged #{number}" for number, _title in seeded.merged if f"#{number}" not in shown
               and not re.search(rf'"issue":\s*{number}\b', shown)]
    missing += [f"merged title {title!r}" for _number, title in seeded.merged if title not in shown]
    missing += [f"released hold #{number} ({say})" for number, say in seeded.holds
                if say not in shown or (f"#{number}" not in shown
                                        and not re.search(rf'"issue":\s*{number}\b', shown))]
    for _question, text, options in seeded.questions:
        missing += [f"question {text!r}"] if text not in shown else []
        missing += [f"option {option!r}" for option in options if option not in shown]
    missing += [f"needs-human #{number}" for number, _title in seeded.persons
                if f"#{number}" not in shown and not re.search(rf'"issue":\s*{number}\b', shown)]
    missing += [f"needs-human title {title!r}" for _number, title in seeded.persons if title not in shown]
    tried = "\n".join(shlex.join(command) for command in commands)
    assert not missing, ("Nothing the old report showed is lost: the CLI still shows it on request. "
                         f"Missing: {missing}\nTried:\n{tried}")


def test_first_run_greeting_with_the_big_squirrel_is_unchanged(short, tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    output = short.brief("--report", at=empty)
    assert output == FIRST_RUN_GREETING, (
        "The first-run report prints the big squirrel and the greeting, unchanged")
    assert output.count("Hi, I'm Foragent.") == 1


def test_report_time_is_recorded_exactly_once_per_run(short, monkeypatch):
    """Observe commits to the report file, so missing and duplicate writes both fail."""
    replaced = []
    replace = os.replace
    record = short.store.root / "report.json"

    def observe(source, destination, *args, **kwargs):
        result = replace(source, destination, *args, **kwargs)
        if Path(destination) == record:
            replaced.append(record.read_bytes())
        return result

    monkeypatch.setattr(os, "replace", observe)
    for index in range(3):
        short.clock[0] = NOW + timedelta(minutes=10 * index)
        before = len(replaced)
        short.report()
        assert len(replaced) - before == 1, "Each report records its time exactly once"
        assert contains_time(replaced[-1], short.clock[0]), "The record holds this run's time"


# --- /fa skill -----------------------------------------------------------------------------

def _skill():
    return (REPO / "skills/fa/SKILL.md").read_text(encoding="utf-8")


def _steps(skill):
    return re.split(r"\n(?=\d+\.\s+\*\*)", skill)


def test_fa_skill_shows_the_short_report():
    steps = _steps(_skill())
    report = [step for step in steps if "fa brief --report" in step]
    assert report, "/fa still runs fa brief --report"
    step = report[0]
    assert re.search(r"(?:show|display|present|relay)", step, re.I), "/fa shows the report"
    assert re.search(r"three lines|3 lines|three-line|세 줄|short report|짧은 보고", step, re.I), (
        "/fa step 1 describes the short (three-line) report")
    assert not re.search(r"what finished since the previous report", step, re.I), (
        "Step 1 no longer describes the long report with its lists")


def test_fa_skill_asks_each_open_decision_as_its_own_multiple_choice_question():
    steps = _steps(_skill())

    def covers(step):
        return (re.search(r"AskUserQuestion|question tool", step)
                and re.search(r"fa orch questions|orchestrator question", step, re.I)
                and re.search(r"needs-human", step)
                and re.search(r"one (?:decision )?per question|one question per decision|"
                              r"each decision[^\n.]{0,60}(?:its own|separate)|one (?:decision )?at a time|"
                              r"하나씩", step, re.I)
                and re.search(r"recommend[^\n]{0,80}first|first[^\n]{0,80}recommend|추천[^\n]{0,40}먼저", step, re.I)
                and re.search(r"option|choice|선택지", step, re.I)
                and "fa orch answer" in step
                and "fa inspect answer" in step)

    assert any(covers(step) for step in steps), (
        "/fa brings every open decision (orchestrator question and needs-human issue) as its own "
        "multiple-choice question with the gate's options, recommendation first, and records each "
        "answer where it came from (fa orch answer / fa inspect answer)")


def test_fa_skill_asks_decisions_after_the_report_not_pasted_as_text():
    skill = _skill()
    steps = _steps(skill)
    report_at = next(i for i, step in enumerate(steps) if "fa brief --report" in step)
    asking = [i for i, step in enumerate(steps)
              if "needs-human" in step and re.search(r"AskUserQuestion|question tool", step)
              and "fa orch answer" in step]
    assert asking and min(asking) > report_at, "Decisions are asked after the short report is shown"
    assert re.search(r"never[^\n.]{0,80}(?:paste|block of text|list)|not[^\n.]{0,40}(?:paste|block of text)|"
                     r"붙여", skill, re.I), "/fa says decisions are never pasted as a block of text"


def test_d126_is_recorded_in_the_decisions_document():
    decisions = (REPO / "docs/decisions.md").read_text(encoding="utf-8")
    assert re.search(r"^#+\s*D127\b.*(/fa|report|보고)", decisions, re.M), "docs/decisions.md records D127 about the /fa report"
