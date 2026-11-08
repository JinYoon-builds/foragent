"""Acceptance: invalid review/checks settings are diagnosable and repairable (n78).

Everything runs through `fa` (`cli.main`): `fa dashboard --once` and `fa init`.
`fa init` uses the offline setup fixture from tests/test_init.py, so setup
prerequisites (git, gh login, repository link) are healthy and fake.
"""

import re

import pytest
import yaml

from foragent import cli
from foragent.commands import init as init_command
from foragent.config import load_config

from test_init import setup  # noqa: F401  (pytest fixture)


UNRELATED = (
    "# Keep my choices\n"
    "executor: claude\n"
    "reviewer: codex\n"
    "language: en\n"
    "max_retries: 4\n"
    "test_command: uv run pytest -q\n"
)
UNRELATED_VALUES = {"executor": "claude", "reviewer": "codex", "language": "en",
                    "max_retries": 4, "test_command": "uv run pytest -q"}
REMOVAL = re.compile(r"\bremoved\b|no longer|retired|was dropped|deprecated", re.IGNORECASE)


def run_fa(argv, capsys):
    """Run `fa <argv>`; argparse refusals (exit 2) count as a nonzero exit, not a crash."""
    capsys.readouterr()
    try:
        code = cli.main(argv)
    except SystemExit as exc:  # e.g. an unknown option on current main
        code = exc.code if isinstance(exc.code, int) else 1
    captured = capsys.readouterr()
    return code, captured.out, captured.err


@pytest.fixture
def fa_init(setup, monkeypatch, capsys):  # noqa: F811
    """`fa init ...` against the offline project; interactive offers are answered off."""
    monkeypatch.setattr(cli, "init_repo", lambda root, **kwargs: setup.init(**kwargs))
    monkeypatch.setattr(cli, "_offer_statusline", lambda answer: None)
    monkeypatch.setattr(cli, "_open_lane_view", lambda root: None)
    monkeypatch.setattr(init_command, "_offer_fa_allow", lambda root, answer: None)
    monkeypatch.setattr(init_command, "_offer_upstream_reports", lambda root: None)
    monkeypatch.setattr(init_command, "_offer_starter", lambda *args: None)

    def no_prompt(prompt=""):
        raise EOFError

    monkeypatch.setattr("builtins.input", no_prompt)

    def run(*flags):
        argv = ["init", "--repo-root", str(setup.root), "--no-push", "--no-launchd", "--no-board", *flags]
        return run_fa(argv, capsys)

    run.root = setup.root
    run.config = setup.root / ".foragent.yml"
    return run


def dashboard(root, capsys):
    return run_fa(["dashboard", "--once", "--repo-root", str(root)], capsys)


def lines_for(text, key):
    return [line for line in text.splitlines() if re.match(rf"{key}\s*:", line)]


def assert_names(message, *words):
    folded = message.lower()
    for word in words:
        assert word.lower() in folded, f"{word!r} missing from diagnostic: {message!r}"


def assert_review_batch_diagnostic(code, err):
    assert code != 0
    assert_names(err, ".foragent.yml", "review", "batch", "fast", "careful")
    assert REMOVAL.search(err), f"diagnostic does not say review batch was removed: {err!r}"


def recovery_choice(message):
    """The review choice the diagnostic tells the person to set, by command or by edit."""
    command = re.search(r"fa init\b[^\n`]*?--review[ =](fast|careful)\b", message)
    if command:
        return "command", command.group(1)
    edit = re.search(r"\breview:\s*(fast|careful)\b", message)
    if edit:
        return "edit", edit.group(1)
    return None


def assert_repaired(path, expected):
    text = path.read_text(encoding="utf-8")
    config = load_config(path.parent)
    for key, value in expected.items():
        assert getattr(config, key) == value, (key, text)
        assert len(lines_for(text, key)) == 1, f"{key} must be written exactly once:\n{text}"
    saved = yaml.safe_load(text)
    for key, value in UNRELATED_VALUES.items():
        assert saved.get(key) == value, f"unrelated setting {key} lost or changed:\n{text}"


# Statement 1: review: batch names the file, key, value, removal, choices and a recovery.

def test_dashboard_names_review_batch_and_its_removal(fa_init, capsys):
    fa_init.config.write_text("review: batch\n" + UNRELATED, encoding="utf-8")
    before = fa_init.config.read_bytes()
    code, _out, err = dashboard(fa_init.root, capsys)
    assert_review_batch_diagnostic(code, err)
    assert recovery_choice(err) is not None, f"no followable recovery in: {err!r}"
    assert fa_init.config.read_bytes() == before


def test_init_without_override_names_review_batch_and_leaves_config(fa_init):
    fa_init.config.write_text("review: batch\n" + UNRELATED, encoding="utf-8")
    before = fa_init.config.read_bytes()
    code, out, err = fa_init()
    assert_review_batch_diagnostic(code, out + err)
    assert recovery_choice(out + err) is not None, f"no followable recovery in: {out + err!r}"
    assert fa_init.config.read_bytes() == before


def test_the_dashboard_recovery_for_review_batch_can_be_followed(fa_init, capsys):
    fa_init.config.write_text("review: batch\n" + UNRELATED, encoding="utf-8")
    code, _out, err = dashboard(fa_init.root, capsys)
    assert code != 0
    found = recovery_choice(err)
    assert found is not None, f"no followable recovery in: {err!r}"
    kind, choice = found
    if kind == "command":
        code, out, err = fa_init("--review", choice)
        assert code == 0, out + err
    else:
        text = fa_init.config.read_text(encoding="utf-8")
        fa_init.config.write_text(text.replace("review: batch", f"review: {choice}"), encoding="utf-8")
    assert_repaired(fa_init.config, {"review": choice})


# Statement 2: explicit init choices repair the matching invalid setting.

@pytest.mark.parametrize("bad,flags,expected", [
    ("review: batch\n", ["--review", "fast"], {"review": "fast"}),
    ("review: sloppy\n", ["--review", "careful"], {"review": "careful"}),
    ("checks: nightly\n", ["--checks", "ci"], {"checks": "ci"}),
    ("checks: remote\n", ["--checks", "local"], {"checks": "local"}),
    ("review: batch\nchecks: nightly\n", ["--review", "careful", "--checks", "ci"],
     {"review": "careful", "checks": "ci"}),
], ids=["legacy-batch", "arbitrary-review", "arbitrary-checks", "checks-to-local", "both-together"])
def test_init_choice_replaces_the_invalid_setting_and_keeps_the_rest(fa_init, bad, flags, expected):
    fa_init.config.write_text(bad + UNRELATED, encoding="utf-8")
    code, out, err = fa_init(*flags)
    assert code == 0, f"exit {code}: {out}{err}"
    assert_repaired(fa_init.config, expected)


# Statement 3: invalid checks is named; overriding a different key does not hide or half-repair it.

def assert_checks_diagnostic(code, message, value="nightly"):
    assert code != 0
    assert_names(message, ".foragent.yml", "checks", value, "local", "ci")
    # "ci" alone is too easy to find inside other words; require it as a word.
    assert re.search(r"\bci\b", message), message


def test_dashboard_names_the_invalid_checks_value_and_file(fa_init, capsys):
    fa_init.config.write_text("checks: nightly\n" + UNRELATED, encoding="utf-8")
    before = fa_init.config.read_bytes()
    code, _out, err = dashboard(fa_init.root, capsys)
    assert_checks_diagnostic(code, err)
    assert fa_init.config.read_bytes() == before


def test_init_without_override_names_the_invalid_checks_value(fa_init):
    fa_init.config.write_text("checks: nightly\n" + UNRELATED, encoding="utf-8")
    before = fa_init.config.read_bytes()
    code, out, err = fa_init()
    assert_checks_diagnostic(code, out + err)
    assert fa_init.config.read_bytes() == before


def test_review_override_does_not_discard_invalid_checks(fa_init):
    fa_init.config.write_text("checks: nightly\n" + UNRELATED, encoding="utf-8")
    before = fa_init.config.read_bytes()
    code, out, err = fa_init("--review", "careful")
    assert_checks_diagnostic(code, out + err)
    assert fa_init.config.read_bytes() == before, "a refused repair must leave the config bytes unchanged"


def test_checks_override_does_not_half_repair_invalid_review(fa_init):
    fa_init.config.write_text("review: batch\nchecks: nightly\n" + UNRELATED, encoding="utf-8")
    before = fa_init.config.read_bytes()
    code, out, err = fa_init("--checks", "ci")
    assert code != 0
    assert_names(out + err, ".foragent.yml", "review", "batch")
    assert fa_init.config.read_bytes() == before, "no partially repaired config may be written"
