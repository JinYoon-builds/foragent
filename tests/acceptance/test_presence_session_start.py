"""Acceptance test for presence-session-start. Locked by Foragent; do not edit."""

from datetime import UTC, datetime, timedelta
from io import BytesIO, TextIOWrapper
import json
from pathlib import Path
import re
import shlex
import sys
from types import SimpleNamespace

import pytest
import yaml

from test_fa_report_since import contains_time, report_case
from test_skills_autosync import cli_exit, isolated_home


NOW = datetime(2026, 10, 27, 12, tzinfo=UTC)
SGR = re.compile(r"\x1b\[([0-9;]*)m")


def _cli_argv(command):
    """Resolve the advertised fa entry point without executing a shell."""
    words = shlex.split(command)
    if words and Path(words[0]).name in {"fa", "foragent"}:
        return words[1:]
    if (len(words) >= 4 and Path(words[0]).name.startswith("python")
            and words[1:3] == ["-m", "foragent.cli"]):
        return words[3:]
    return None


def _settings_commands(settings):
    for group in settings.get("hooks", {}).get("SessionStart", []):
        for hook in group.get("hooks", []):
            if hook.get("type") == "command" and isinstance(hook.get("command"), str):
                yield hook["command"]


def _session_start_argv(root, settings_path):
    """Use installed settings or an actual documented command; invent no CLI name."""
    for path in (settings_path, root / ".claude/settings.json", root / ".claude/settings.local.json"):
        if path.is_file():
            for command in _settings_commands(json.loads(path.read_text(encoding="utf-8"))):
                if argv := _cli_argv(command):
                    return argv
    source = Path(__file__).resolve().parents[2]
    documents = [source / "README.md", *sorted((source / "docs").rglob("*.md")),
                 *sorted((source / "skills").rglob("*.md"))]
    for path in documents:
        content = path.read_text(encoding="utf-8")
        if "SessionStart" not in content:
            continue
        for block in re.findall(r"```(?:json)?\s*\n(.*?)```", content, re.S):
            try:
                settings = json.loads(block)
            except ValueError:
                continue
            if isinstance(settings, dict):
                for command in _settings_commands(settings):
                    if argv := _cli_argv(command):
                        return argv
        # A prose example may give the command inline instead of settings JSON.
        for paragraph in re.split(r"\n\s*\n", content):
            if "SessionStart" in paragraph:
                for command in re.findall(r"`([^`\n]+)`", paragraph):
                    if argv := _cli_argv(command):
                        return argv
    raise AssertionError("Foragent must install or document a Claude Code SessionStart command")


@pytest.fixture
def session_case(report_case, monkeypatch, capsys):
    from foragent import cli, statusline
    from foragent.init import init_repo
    from foragent.tracker import FakeTracker, Issue

    case = report_case
    settings_path = statusline.claude_settings_path()
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps({"statusLine": {
        "type": "command", "command": "offline-user-statusline"}}), encoding="utf-8")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(settings_path.parent))
    case.configure(checks="local")
    case.store.record_issue(Issue(401, case.question["question"], "",
                                 frozenset({"needs-human", "lane:decision"}), "open"),
                            {"api": {}, "decision": {}})
    global_path = Path.home() / ".config/foragent/config.yml"
    global_config = yaml.safe_load(global_path.read_text(encoding="utf-8"))
    global_path.write_text(yaml.safe_dump({**global_config, "upstream_reports": False}), encoding="utf-8")

    # Keep the real init and installation paths. Only Git, GitHub and launchd
    # dependencies are fakes; no commits, network or background jobs are run.
    git_calls = []
    git = SimpleNamespace(
        check_worktree=lambda: None, worktree_root=lambda: case.root,
        remote_slug=lambda: "offline/reports", uncommitted_paths=lambda paths: [],
        current_branch=lambda: "main",
        commit_paths=lambda paths, message: git_calls.append((paths, message)),
    )

    def fake_gh(argv, input=None):
        assert argv == ["auth", "status"], f"Unexpected setup GitHub request: {argv}"
        return ""

    monkeypatch.setattr(cli, "init_repo", lambda root, **kwargs: init_repo(
        root, git=git, tracker=FakeTracker(), gh_run=fake_gh, **kwargs))
    code = cli_exit(cli.main, ["init", "--repo-root", str(case.root), "--statusline", "yes",
                               "--no-push", "--no-board", "--no-launchd"])
    captured = capsys.readouterr()
    assert code == 0, f"Offline fa init must exercise the real hook installer: {captured.err}"
    assert case.gh_calls == [], "Hook setup must not call a real GitHub command"

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return case.clock[0].astimezone(tz) if tz else case.clock[0].replace(tzinfo=None)

    # Include the hook's loaded domain module, not only the CLI facade. New
    # readers still receive report_case's injected StateStore clock.
    for name, module in tuple(sys.modules.items()):
        if name.startswith("foragent.") and getattr(module, "datetime", None) is datetime:
            monkeypatch.setattr(module, "datetime", Clock)

    def invoke(argv, payload):
        streams = [TextIOWrapper(BytesIO(), encoding="utf-8") for _ in range(2)]
        stdout, stderr = streams
        stdin = TextIOWrapper(BytesIO(json.dumps(payload).encode("utf-8")), encoding="utf-8")
        with monkeypatch.context() as redirected:
            redirected.setattr(sys, "stdin", stdin)
            redirected.setattr(sys, "stdout", stdout)
            redirected.setattr(sys, "stderr", stderr)
            code = cli_exit(cli.main, argv)
            stdout.flush()
            stderr.flush()
            output, error = stdout.buffer.getvalue(), stderr.buffer.getvalue()
        assert case.gh_calls == [], "SessionStart and its statusline must make zero GitHub calls"
        assert code == 0, f"Advertised entry point fa {shlex.join(argv)} must succeed: {error!r}"
        assert not error, "Presence must not print advice or banners on stderr"
        return output.decode("utf-8")

    def hook(session_id="offline-session-A"):
        argv = _session_start_argv(case.root, settings_path)
        output = invoke(argv, {"hook_event_name": "SessionStart", "source": "startup",
                               "session_id": session_id, "cwd": str(case.root),
                               "transcript_path": str(case.root / f"{session_id}.jsonl")})
        if not output.strip():
            return {}
        try:
            result = json.loads(output)
        except ValueError:
            pytest.fail("SessionStart stdout must be one JSON object, without banner art or other text")
        assert isinstance(result, dict), "SessionStart must print a JSON object"
        assert "▀" not in output and "▄" not in output, "SessionStart plain stdout must carry no banner art"
        return result

    def status(session_id="offline-session-A"):
        return invoke(["statusline"], {"session_id": session_id, "cwd": str(case.root),
                                      "workspace": {"current_dir": str(case.root),
                                                    "project_dir": str(case.root)}})

    case.hook = hook
    case.hook_argv = lambda: _session_start_argv(case.root, settings_path)
    case.status = status
    yield case
    assert case.gh_calls == [], "SessionStart must make zero GitHub calls, including failed runs"


def _local_files(case):
    from foragent import statusline

    roots = (case.store.root, statusline.data_dir(), case.root / ".foragent")
    return {path: path.read_bytes() for root in roots for path in root.rglob("*") if path.is_file()}


def _assert_message(payload, merges, language):
    message = payload.get("systemMessage")
    assert isinstance(message, str) and message.strip(), "SessionStart must supply its one-line systemMessage"
    assert len(message.splitlines()) == 1, "SessionStart systemMessage must be one line"
    assert "▀" not in message and "▄" not in message, "The hook message must contain no squirrel banner"
    if language == "en":
        assert message == f"since the last report: {merges} merges · ✋ 1 · /fa for the report", (
            "Show merges since the last report, the person's-turn count and /fa in one line")
    else:
        assert re.search(r"(?:지난|마지막)\s*보고\s*(?:이후|뒤)", message), "Use Korean for since the last report"
        assert re.search(r"머지|병합", message), "Name the merge count in Korean"
        assert re.search(rf"(?<!\d){merges}(?!\d)", message.split("✋")[0]), "Use the merge count since the report"
        assert re.search(r"✋\s*1(?!\d)", message), "Show the one open person's turn"
        assert "/fa" in message and "보고" in message.split("/fa", 1)[1], "Point to /fa for the Korean report"


def _rgb(index):
    if index >= 232:
        return (8 + 10 * (index - 232),) * 3
    levels = (0, 95, 135, 175, 215, 255)
    value = index - 16
    return levels[value // 36], levels[value // 6 % 6], levels[value % 6]


def _colored_cells(line):
    """Decode SGR cells so grouping, resets and truecolor/256-color are free."""
    cells, foreground, background, reverse, offset = [], None, None, False, 0
    for match in SGR.finditer(line):
        colors = (background, foreground) if reverse else (foreground, background)
        cells.extend((glyph, *colors) for glyph in line[offset:match.start()])
        codes = [int(code or 0) for code in match.group(1).split(";")]
        at = 0
        while at < len(codes):
            code = codes[at]
            if code == 0:
                foreground, background, reverse = None, None, False
            elif code in (7, 27):
                reverse = code == 7
            elif code == 39:
                foreground = None
            elif code == 49:
                background = None
            elif code in (38, 48):
                mode = codes[at + 1]
                assert mode in (2, 5), "Use a standard truecolor or 256-color SGR sequence"
                color = _rgb(codes[at + 2]) if mode == 5 else tuple(codes[at + 2:at + 5])
                if code == 38:
                    foreground = color
                else:
                    background = color
                at += 2 if mode == 5 else 4
            at += 1
        offset = match.end()
    colors = (background, foreground) if reverse else (foreground, background)
    cells.extend((glyph, *colors) for glyph in line[offset:])
    return cells


def _assert_squirrel(output):
    from foragent import watch_art

    rows = watch_art.half_blocks(watch_art.ORCHESTRATOR, watch_art.PALETTE)
    lines = output.splitlines()
    painted = [index for index, line in enumerate(lines) if "▀" in line or "▄" in line]
    assert len(painted) == len(rows), "Keep all eight rows of the existing big squirrel"
    assert painted == list(range(painted[0], painted[0] + len(rows)))
    for index, row in zip(painted, rows):
        cells = _colored_cells(lines[index])
        expected_width = len("".join(glyph for glyph, _, _ in row).rstrip())
        assert len(cells) == expected_width
        for (glyph, foreground, background), (_, upper, lower) in zip(cells, row):
            expected_upper = None if upper is None else _rgb(upper)
            expected_lower = None if lower is None else _rgb(lower)
            if upper is None and lower is not None:
                assert glyph == "▄", "r52: bottom-only pixels must not inherit the terminal foreground"
            if glyph == "▀":
                actual_upper, actual_lower = foreground, background
            elif glyph == "▄":
                actual_upper, actual_lower = background, foreground
            else:
                assert glyph == " "
                actual_upper = actual_lower = background
            assert (actual_upper, actual_lower) == (expected_upper, expected_lower), "Keep both palette pixels and transparent halves"
    assert painted[-1] < len(lines) - 1
    _assert_status_segment(lines[-1])


def _assert_status_segment(line):
    plain = SGR.sub("", line)
    assert plain.startswith("Claude"), "Preserve the user's own statusline output"
    assert "🐿" in plain and re.search(r"1\s+lanes? working", plain), "Keep the local one-line presence segment"
    assert re.search(r"✋\s*1(?!\d)", plain), "Keep the person's-turn count in the statusline"


@pytest.mark.parametrize("language", ["en", "ko"])
def test_session_start_prints_local_json_counts_since_report_without_consuming_it(session_case, language):
    case = session_case
    case.hook_argv()
    case.configure(language=language)
    case.brief("--report")
    case.merge(104, NOW + timedelta(minutes=5))
    case.merge(105, NOW + timedelta(minutes=7))
    case.clock[0] = NOW + timedelta(minutes=10)
    _assert_message(case.hook(), 2, language)
    _assert_message(case.hook("offline-session-B"), 2, language)
    report = case.brief("--report")
    assert "#104" in report and "#105" in report, "SessionStart must not consume or advance the report cursor"
    _assert_message(case.hook("offline-session-C"), 0, language)


def test_statusline_shows_existing_colored_squirrel_only_during_first_sixty_seconds(session_case):
    case = session_case
    case.hook()
    for seconds in (0, 59, 60, 61):
        case.clock[0] = NOW + timedelta(seconds=seconds)
        output = case.status()
        if seconds < 60:
            _assert_squirrel(output)
        else:
            assert "▀" not in output and "▄" not in output, "At 60 seconds the squirrel must disappear"
            assert len(output.splitlines()) == 1, "After 60 seconds show only the one-line status segment"
            _assert_status_segment(output)


def test_session_start_is_the_only_writer_of_session_time_and_refreshes_do_not_extend_it(session_case):
    case = session_case
    before = _local_files(case)
    case.hook()
    recorded = _local_files(case)
    changed = [path for path in recorded if recorded[path] != before.get(path)]
    assert changed and any(contains_time(recorded[path], NOW) for path in changed), (
        "The SessionStart hook must persist the session's start time in local state")
    for seconds in (20, 50):
        case.clock[0] = NOW + timedelta(seconds=seconds)
        _assert_squirrel(case.status())
        case.brief()
        assert _local_files(case) == recorded, "Brief and statusline must not rewrite the hook's session start time"
    case.clock[0] = NOW + timedelta(seconds=61)
    assert "▀" not in case.status(), "Repeated statusline reads must not restart the sixty-second window"
    case.clock[0] = NOW + timedelta(minutes=2)
    case.hook("offline-session-B")
    restarted = _local_files(case)
    assert any(contains_time(restarted[path], case.clock[0])
               for path in restarted if restarted[path] != recorded.get(path)), "A new SessionStart records its own time"
    _assert_squirrel(case.status("offline-session-B"))


@pytest.mark.parametrize("statusline_enabled", [True, False], ids=["keep-segment", "no-segment"])
def test_session_start_off_switch_suppresses_message_and_squirrel_independently_of_segment(
    session_case, statusline_enabled,
):
    case = session_case
    case.hook()
    _assert_squirrel(case.status())
    case.configure(presence={"session_start": False, "statusline": statusline_enabled})
    before = _local_files(case)
    payload = case.hook("offline-session-B")
    assert not payload.get("systemMessage"), "presence.session_start: false must suppress systemMessage"
    for session_id in ("offline-session-A", "offline-session-B"):
        output = case.status(session_id)
        assert "▀" not in output and "▄" not in output, "The off switch must also hide a still-recent squirrel"
        if statusline_enabled:
            assert len(output.splitlines()) == 1
            _assert_status_segment(output)
        else:
            assert output == "Claude", "presence.statusline: false must preserve the user's exact output"
    assert _local_files(case) == before, "A disabled SessionStart and statusline must not record a new session time"
