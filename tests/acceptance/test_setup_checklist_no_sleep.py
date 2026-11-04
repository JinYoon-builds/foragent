"""Acceptance for the setup checklist item "the machine does not idle-sleep while the gate runs".

The checklist (#1799) is one list that `fa doctor`, `fa init` and /fa read. `fa doctor --setup --json`
prints {"items": [{"id", "ok", "fix"}, ...]} in check order.

On 2026-11-04 the new Mac mini had system sleep after 1 minute. The only sleep blockers were per-run
caffeinate assertions, so between runs the machine could sleep and stop gate ticks and remote access.

On macOS the item reads the sleep setting and the power assertions with `pmset -g ...` (read only).
When the machine can sleep, its fix offers a user LaunchAgent that runs `caffeinate -i -s` (no admin
needed, installed only after the person agrees) and points to the Energy settings for "start up
automatically after a power failure". On Linux, or when pmset cannot run, the item never fails:
it is absent or ok.

A fake `pmset` is on PATH in front of the real one. Fake `launchctl`, `caffeinate` and `sudo` log every
call, so the tests prove that doctor and init never install, load or start anything.
"""

import json
import os
from pathlib import Path
import platform
import shutil
import sys

import pytest

from foragent import cli

from conftest import GitHistory, git_command

ITEM = "no-idle-sleep"

SETTINGS_CAN_SLEEP = """System-wide power settings:
Currently in use:
 standby              0
 Sleep On Power Button 1
 autorestart          0
 powernap             1
 disksleep            0
 sleep                1 (sleep prevented by powerd, caffeinate)
 ttyskeepawake        1
 displaysleep         0
 womp                 1
"""

SETTINGS_NEVER_SLEEP = """System-wide power settings:
Currently in use:
 standby              0
 autorestart          0
 disksleep            10
 sleep                0
 displaysleep         10
 womp                 1
"""

# Only per-run blockers: one is tied to a gate process, one has a timeout. Both end between runs.
ASSERTIONS_PER_RUN = """2026-11-04 02:34:49 +0900
Assertion status system-wide:
   BackgroundTask                 0
   UserIsActive                   1
   PreventUserIdleDisplaySleep    0
   PreventSystemSleep             1
   PreventUserIdleSystemSleep     1
   NetworkClientActive            0
Listed by owning process:
   pid 42(powerd): [0x0000384900018bc6] 00:11:07 PreventUserIdleSystemSleep named: "Powerd - Prevent sleep while display is on"
   pid 88906(caffeinate): [0x00002fb00001a39a] 00:47:48 PreventUserIdleSystemSleep named: "caffeinate command-line tool"
\tDetails: caffeinate asserting on behalf of Process ID 22070
\tLocalized=THE CAFFEINATE TOOL IS PREVENTING SLEEP.
   pid 88906(caffeinate): [0x00002fb00007a39b] 00:47:48 PreventSystemSleep named: "caffeinate command-line tool"
\tDetails: caffeinate asserting on behalf of Process ID 22070
\tLocalized=THE CAFFEINATE TOOL IS PREVENTING SLEEP.
   pid 68038(caffeinate): [0x00003aa100018c5e] 00:01:07 PreventUserIdleSystemSleep named: "caffeinate command-line tool"
\tDetails: caffeinate asserting for 300 secs
\tLocalized=THE CAFFEINATE TOOL IS PREVENTING SLEEP.
\tTimeout will fire in 233 secs Action=TimeoutActionRelease
"""

# A standing `caffeinate -i -s` (what the offered LaunchAgent runs) keeps the machine awake.
ASSERTIONS_STANDING = ASSERTIONS_PER_RUN + """   pid 70699(caffeinate): [0x00003af000018c66] 03:00:01 PreventUserIdleSystemSleep named: "caffeinate command-line tool"
\tDetails: caffeinate asserting forever
\tLocalized=THE CAFFEINATE TOOL IS PREVENTING SLEEP.
   pid 70699(caffeinate): [0x00003af000078c67] 03:00:01 PreventSystemSleep named: "caffeinate command-line tool"
\tDetails: caffeinate asserting forever
\tLocalized=THE CAFFEINATE TOOL IS PREVENTING SLEEP.
"""

ASSERTIONS_NONE = """2026-11-04 02:34:49 +0900
Assertion status system-wide:
   PreventSystemSleep             0
   PreventUserIdleSystemSleep     0
Listed by owning process:
"""

FAKE_PMSET = r'''
import json, os, sys
args = sys.argv[1:]
with open(os.environ["FAKE_CALL_LOG"], "a", encoding="utf-8") as log:
    log.write(json.dumps({"tool": "pmset", "args": args}) + "\n")
if os.environ.get("FAKE_PMSET_FAIL"):
    print("pmset: could not read power settings", file=sys.stderr)
    sys.exit(1)
if not args or args[0] != "-g":
    sys.exit(1)
if "assertions" in args:
    sys.stdout.write(open(os.environ["FAKE_PMSET_ASSERTIONS"], encoding="utf-8").read())
else:
    sys.stdout.write(open(os.environ["FAKE_PMSET_SETTINGS"], encoding="utf-8").read())
'''

FAKE_LOGGED = r'''
import json, os, sys
with open(os.environ["FAKE_CALL_LOG"], "a", encoding="utf-8") as log:
    log.write(json.dumps({"tool": os.path.basename(sys.argv[0]), "args": sys.argv[1:]}) + "\n")
'''

FAKE_GH = r'''
import json, sys
args = sys.argv[1:]
if args[:2] == ["auth", "status"]:
    print("github.com")
    print("  ✓ Logged in to github.com account human (keyring)")
    print("  - Token: gho_************************************")
    print("  - Token scopes: 'gist', 'project', 'read:org', 'repo', 'workflow'")
elif args[:2] == ["auth", "token"]:
    print("gho_offline_token")
elif args[:2] == ["repo", "view"]:
    print(json.dumps({"isPrivate": True}))
elif args[:2] == ["api", "user"]:
    print(json.dumps({"login": "human", "type": "User"}))
else:
    print("[]")
'''

FAKE_TOOL = r'''
import sys
args = sys.argv[1:]
if args[:2] == ["auth", "status"]:
    print('{"loggedIn": true}')
elif args[:2] == ["login", "status"]:
    print("Logged in using ChatGPT")
'''


@pytest.fixture
def machine(tmp_path, monkeypatch):
    for key, value in (("GIT_TERMINAL_PROMPT", "0"), ("FORAGENT_UPDATE_CHECK", "off"),
                       ("FORAGENT_ORCH_SPAWN", "off"),
                       ("GIT_AUTHOR_NAME", "Test Human"), ("GIT_AUTHOR_EMAIL", "human@example.com"),
                       ("GIT_COMMITTER_NAME", "Test Human"), ("GIT_COMMITTER_EMAIL", "human@example.com")):
        monkeypatch.setenv(key, value)
    for name in ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "ANTHROPIC_API_KEY",
                 "CLAUDE_CODE_OAUTH_TOKEN", "FAKE_PMSET_FAIL"):
        monkeypatch.delenv(name, raising=False)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "git").symlink_to(shutil.which("git"))
    fakes = {"gh": FAKE_GH, "claude": FAKE_TOOL, "codex": FAKE_TOOL, "pmset": FAKE_PMSET,
             "launchctl": FAKE_LOGGED, "caffeinate": FAKE_LOGGED, "sudo": FAKE_LOGGED}
    for name, source in fakes.items():
        tool = bin_dir / name
        tool.write_text(f"#!{sys.executable}\n{source}", encoding="utf-8")
        tool.chmod(0o755)
    log = tmp_path / "calls.jsonl"
    settings, assertions = tmp_path / "pmset-settings.txt", tmp_path / "pmset-assertions.txt"
    monkeypatch.setenv("FAKE_CALL_LOG", str(log))
    monkeypatch.setenv("FAKE_PMSET_SETTINGS", str(settings))
    monkeypatch.setenv("FAKE_PMSET_ASSERTIONS", str(assertions))
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + "/usr/bin" + os.pathsep + "/bin")

    origin, repo = tmp_path / "origin.git", tmp_path / "project"
    git_command(tmp_path, "init", "--bare", "--initial-branch=main", str(origin))
    GitHistory(origin, repo).update({"README.md": "# Demo\n"})
    git_command(tmp_path, "clone", "-q", str(origin), str(repo))
    git_command(repo, "remote", "set-url", "origin", "https://github.com/owner/repo.git")

    class Machine:
        root = repo
        home = Path(os.environ["HOME"])

        @staticmethod
        def os(name: str) -> None:
            monkeypatch.setattr(sys, "platform", {"mac": "darwin", "linux": "linux"}[name])
            monkeypatch.setattr(platform, "system", lambda: {"mac": "Darwin", "linux": "Linux"}[name])

        @staticmethod
        def power(settings_text: str, assertions_text: str) -> None:
            settings.write_text(settings_text, encoding="utf-8")
            assertions.write_text(assertions_text, encoding="utf-8")

        @staticmethod
        def calls() -> list[dict]:
            if not log.is_file():
                return []
            return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line]

    Machine.os("mac")
    Machine.power(SETTINGS_CAN_SLEEP, ASSERTIONS_PER_RUN)
    return Machine


def run(argv, capsys) -> tuple[int, str, str]:
    capsys.readouterr()
    try:
        code = cli.main(argv)
    except SystemExit as exc:
        code = exc.code
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def doctor_items(machine, capsys) -> tuple[int, list[dict]]:
    code, out, err = run(["doctor", "--setup", "--json", "--repo-root", str(machine.root)], capsys)
    assert "Traceback" not in out + err
    try:
        report = json.loads(out)
    except json.JSONDecodeError:
        pytest.fail(f"`fa doctor --setup --json` must print one JSON object on stdout: {out!r} {err!r}")
    items = report.get("items") if isinstance(report, dict) else None
    assert isinstance(items, list) and items, f"The checklist report needs a non-empty items list: {report!r}"
    ids = [item.get("id") for item in items]
    assert len(ids) == len(set(ids)), f"Item ids must be unique: {ids}"
    return code, items


def sleep_items(items: list[dict]) -> list[dict]:
    return [item for item in items if item.get("id") == ITEM]


def tree(root: Path) -> dict[str, bytes]:
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*")
            if p.is_file() and ".git" not in p.relative_to(root).parts}


def side_effects(calls: list[dict]) -> list[dict]:
    """Calls that change the machine: anything but a read-only `pmset -g ...`."""
    return [call for call in calls if call["tool"] != "pmset" or call["args"][:1] != ["-g"]]


def test_mac_that_sleeps_between_runs_fails_with_the_launch_agent_and_power_failure_fix(machine, capsys):
    before = tree(machine.root), tree(machine.home)

    code, items = doctor_items(machine, capsys)

    found = sleep_items(items)
    assert len(found) == 1, f"The checklist holds one {ITEM} item on macOS: {items!r}"
    item = found[0]
    assert item["ok"] is False, f"System sleep 1 with only per-run blockers can sleep between runs: {item!r}"
    fix = item.get("fix")
    assert isinstance(fix, str), f"A failing item names its fix: {item!r}"
    assert "caffeinate -i -s" in fix, f"The fix offers a standing caffeinate: {fix!r}"
    assert "launchagent" in fix.lower(), f"The fix offers a user LaunchAgent: {fix!r}"
    assert "energy" in fix.lower() and "power failure" in fix.lower(), \
        f"The fix points to Energy settings for start up after a power failure: {fix!r}"
    assert "sudo" not in fix, f"The offered fix needs no admin rights: {fix!r}"
    assert code != 0, "doctor exits non-zero when an item fails"
    assert any(call["tool"] == "pmset" for call in machine.calls()), "The item reads pmset"
    assert side_effects(machine.calls()) == [], "doctor installs, loads and starts nothing"
    assert (tree(machine.root), tree(machine.home)) == before, "doctor --setup only reads"


@pytest.mark.parametrize("settings_text, assertions_text", [
    (SETTINGS_NEVER_SLEEP, ASSERTIONS_NONE),
    (SETTINGS_CAN_SLEEP, ASSERTIONS_STANDING),
], ids=["system-sleep-never", "standing-caffeinate"])
def test_mac_that_stays_awake_passes(machine, capsys, settings_text, assertions_text):
    machine.power(settings_text, assertions_text)

    _code, items = doctor_items(machine, capsys)

    found = sleep_items(items)
    assert len(found) == 1, f"The checklist holds one {ITEM} item on macOS: {items!r}"
    assert found[0]["ok"] is True, f"A machine that cannot idle-sleep passes: {found[0]!r}"
    assert side_effects(machine.calls()) == []


@pytest.mark.parametrize("case", ["linux", "pmset-fails"])
def test_item_never_fails_where_it_cannot_check(machine, capsys, monkeypatch, case):
    if case == "linux":
        machine.os("linux")  # pmset still answers "sleep 1": the item must not trust it off macOS.
    else:
        monkeypatch.setenv("FAKE_PMSET_FAIL", "1")

    _code, items = doctor_items(machine, capsys)

    found = sleep_items(items)
    assert len(found) <= 1
    assert all(item["ok"] is True for item in found), \
        f"Where sleep cannot be checked the item is absent or ok, never a misleading failure: {found!r}"
    assert side_effects(machine.calls()) == []


def test_init_shows_the_same_fix_and_installs_nothing(machine, capsys):
    _code, items = doctor_items(machine, capsys)
    found = sleep_items(items)
    assert len(found) == 1 and found[0]["ok"] is False
    agents = machine.home / "Library" / "LaunchAgents"
    agents_before = tree(agents) if agents.is_dir() else {}

    _code, out, err = run(["init", "--repo-root", str(machine.root), "--approver", "human", "--language", "en",
                           "--no-push", "--no-launchd", "--statusline", "no", "--dry-run"], capsys)

    output = out + err
    assert "Traceback" not in output
    assert found[0]["fix"] in output, f"init shows the same fix as doctor: {output!r}"
    assert side_effects(machine.calls()) == [], "init installs, loads and starts nothing without consent"
    assert (tree(agents) if agents.is_dir() else {}) == agents_before, "init writes no LaunchAgent"
