"""Acceptance: `fa init` shows an incomplete setup the way `fa doctor --setup` shows it.

UX research (r813) found two problems when `fa init --dry-run` stops on the setup checklist:
- In a folder that is not a git work tree, the multi-line repository fix is flattened onto one
  line, while `fa doctor --setup` in the same folder prints one step per line.
- With gh logged out, one line mixes the raw subprocess error ("Command [...] returned non-zero
  exit status 1"), gh's own stderr, two languages and `gh auth login` three times.

Contract locked here (WHAT only, D98):
- When init stops on an incomplete setup, every step line of each failing checklist fix that
  `fa doctor --setup` prints is its own line in init's output, in the same order of steps.
- The whole message is in the configured language (the --language flag, else .foragent.yml);
  doctor's lines in that folder are in the same language.
- Each command to run appears once; exactly one line mentions `fa init`, and it is the last
  line: rerun fa init.
- No raw subprocess or exception text (no "returned non-zero exit status", no gh stderr).
The exact wording, prefixes and whether init repeats doctor's PASS/FAIL marks are the lane's.

Reuses the fake-tool machine of the setup checklist locks: only fake gh/uv/claude/codex and git
are on PATH, HOME is a temp folder, nothing reaches GitHub.
"""

import re

from test_setup_checklist_tools import machine  # noqa: F401 -- fixture

from foragent import cli

HANGUL = re.compile(r"[가-힣]")
GH_STDERR = "You are not logged into any GitHub hosts"
RAW_ERROR = "returned non-zero exit status"
INIT_FLAGS = ["--dry-run", "--no-push", "--no-launchd", "--statusline", "no"]


def run(argv, capsys) -> tuple[int, str]:
    capsys.readouterr()
    try:
        code = cli.main(argv)
    except SystemExit as exc:
        code = exc.code
    captured = capsys.readouterr()
    return code, captured.out + captured.err


def lines(output: str) -> list[str]:
    return [line.strip() for line in output.splitlines() if line.strip()]


def doctor_failures(root, capsys) -> tuple[str, list[str]]:
    """doctor --setup's text and the step lines of its failing, blocking fixes, in order."""
    _code, output = run(["doctor", "--setup", "--repo-root", str(root)], capsys)
    assert "Traceback" not in output
    steps = []
    for line in output.splitlines():
        if line.startswith("FAIL "):
            steps.append(line.split(": ", 1)[1].strip() if ": " in line else line)
        elif line.strip() and not line.startswith("PASS ") and steps:
            steps.append(line.strip())  # a continuation line of the previous fix
    return output, steps


def assert_shown_like_doctor(output: str, steps: list[str]) -> None:
    shown = lines(output)
    position = -1
    for step in steps:
        found = [i for i, line in enumerate(shown) if line.endswith(step)]
        assert found, f"init shows doctor's step {step!r} on its own line:\n{output}"
        assert found[0] > position, f"init keeps doctor's step order at {step!r}:\n{output}"
        position = found[0]


def assert_one_rerun_line(output: str) -> None:
    shown = lines(output)
    rerun = [line for line in shown if "fa init" in line]
    assert len(rerun) == 1, f"Exactly one line says to rerun fa init: {rerun}\n{output}"
    assert shown[-1] == rerun[0], f"The rerun line is the last line:\n{output}"


def assert_no_raw_error(output: str) -> None:
    assert "Traceback" not in output
    assert RAW_ERROR not in output, f"No raw subprocess error text:\n{output}"
    assert GH_STDERR not in output, f"No raw gh stderr text:\n{output}"


def test_non_git_folder_shows_the_repository_steps_one_per_line(machine, capsys, tmp_path):  # noqa: F811
    folder = tmp_path / "plain"
    folder.mkdir()
    doctor, steps = doctor_failures(folder, capsys)
    step_lines = [s for s in steps if "git init" in s or "gh repo create" in s or "git remote add" in s]
    assert len(step_lines) >= 4, f"doctor names git init and the ways to link it:\n{doctor}"

    code, output = run(["init", "--repo-root", str(folder), "--language", "en", *INIT_FLAGS], capsys)

    assert code != 0, output
    assert "Traceback" not in output
    shown = lines(output)
    for step in step_lines:
        assert sum(line.endswith(step) for line in shown) == 1, \
            f"Each command is its own line, once: {step!r}\n{output}"
    assert_shown_like_doctor(output, step_lines)
    assert_one_rerun_line(output)
    assert not HANGUL.search(output + doctor), f"English throughout:\n{output}\n{doctor}"
    assert not (folder / ".foragent.yml").exists()
    assert not (folder / ".git").exists(), "init creates nothing while it stops"


def test_gh_logged_out_in_english_names_gh_auth_login_once(machine, capsys):  # noqa: F811
    machine.log_out()
    doctor, steps = doctor_failures(machine.root, capsys)
    assert any(step == "gh auth login" for step in steps), doctor

    code, output = run(["init", "--repo-root", str(machine.root), "--language", "en", *INIT_FLAGS], capsys)

    assert code != 0, output
    assert_no_raw_error(output)
    assert output.count("gh auth login") == 1, f"gh auth login is named once:\n{output}"
    assert_shown_like_doctor(output, steps)
    assert_one_rerun_line(output)
    assert not HANGUL.search(output), f"Only English under --language en:\n{output}"
    assert not HANGUL.search(doctor), f"doctor --setup speaks English here too:\n{doctor}"
    assert not (machine.root / ".foragent.yml").exists()


def test_gh_logged_out_in_korean_speaks_only_korean(machine, capsys):  # noqa: F811
    machine.log_out()
    # The configured language: an existing .foragent.yml, no --language flag.
    (machine.root / ".foragent.yml").write_text("repo: owner/repo\nlanguage: ko\n", encoding="utf-8")
    doctor, steps = doctor_failures(machine.root, capsys)
    assert any(step == "gh auth login" for step in steps), doctor

    code, output = run(["init", "--repo-root", str(machine.root), *INIT_FLAGS], capsys)

    assert code != 0, output
    assert_no_raw_error(output)
    assert output.count("gh auth login") == 1, f"gh auth login is named once:\n{output}"
    assert_shown_like_doctor(output, steps)
    assert_one_rerun_line(output)
    for text in (output, doctor):
        for line in lines(text):
            if line.startswith("PASS "):
                continue
            # A line without Korean may only carry a command the person runs.
            assert HANGUL.search(line) or line.endswith("gh auth login"), \
                f"Korean throughout under language ko: {line!r}\n{text}"
