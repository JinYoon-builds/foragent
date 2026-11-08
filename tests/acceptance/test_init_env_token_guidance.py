"""Acceptance (n72): `fa init` explains an invalid GitHub environment token accurately.

Contract locked here (WHAT only):
- An invalid active GH_TOKEN: fa init exits 1, and its English and Korean guidance names GH_TOKEN
  and says to unset or replace it before gh auth login. The secret value is never printed and the
  failure is not called a saved-login token.
- An invalid active GITHUB_TOKEN with no GH_TOKEN: the guidance names GITHUB_TOKEN. With both
  set, the guidance names the source gh actually selected (GH_TOKEN), not the inactive fallback.
- With no environment token supplying the failed credential, the saved-login recovery stays.
  While it reports any of these failures, fa init does not unset variables, replace credentials
  or run an interactive login.

Wording is the lane's. The fake gh behaves like gh 2.x: GH_TOKEN wins over GITHUB_TOKEN for
github.com, and `gh auth status` names the failing source and exits 1. Only fake tools are on
PATH (the setup checklist machine); nothing reaches GitHub.
"""

import os
from pathlib import Path
import re
import sys

import pytest

from test_setup_checklist_tools import machine  # noqa: F401 -- fixture

from foragent import cli

HANGUL = re.compile(r"[가-힣]")
GH_SECRET = "ghp_DUMMYgh0token0SECRET0value0001"
GITHUB_SECRET = "ghp_DUMMYgithub0token0SECRET0value2"
INIT_FLAGS = ["--dry-run", "--no-push", "--no-launchd", "--statusline", "no"]
SAVED_LOGIN = re.compile(r"saved[ -]login|saved login|저장된\s*로그인|저장된", re.IGNORECASE)
UNSET_OR_REPLACE_EN = re.compile(r"\bunset\b|\breplace\b", re.IGNORECASE)
UNSET_OR_REPLACE_KO = re.compile(r"unset|해제|지우|삭제|바꾸|교체")

# gh 2.x: an environment token takes precedence over the keyring; GH_TOKEN over GITHUB_TOKEN.
# FAKE_GH_KEYRING_INVALID=1 makes the saved (keyring) login invalid when no env token is set.
FAKE_GH = r'''
import json, os, sys
args = sys.argv[1:]
with open(os.environ["FAKE_TOOL_LOG"], "a", encoding="utf-8") as log:
    log.write(json.dumps({"tool": "gh", "args": args,
                          "env": {k: os.environ.get(k) for k in ("GH_TOKEN", "GITHUB_TOKEN")}}) + "\n")
source = "GH_TOKEN" if os.environ.get("GH_TOKEN") else "GITHUB_TOKEN" if os.environ.get("GITHUB_TOKEN") else None
keyring_invalid = os.environ.get("FAKE_GH_KEYRING_INVALID") == "1"
if args[:1] in (["--version"], ["version"]):
    print("gh version 2.62.0 (2024-11-14)")
elif args[:2] == ["auth", "status"]:
    if source:
        print("github.com", file=sys.stderr)
        print(f"  X Failed to log in to github.com using token ({source})", file=sys.stderr)
        print("  - Active account: true", file=sys.stderr)
        print(f"  - The token in {source} is invalid.", file=sys.stderr)
        sys.exit(1)
    if keyring_invalid:
        print("github.com", file=sys.stderr)
        print("  X Failed to log in to github.com account human (keyring)", file=sys.stderr)
        print("  - Active account: true", file=sys.stderr)
        print("  - The token in keyring is invalid.", file=sys.stderr)
        print("  - To re-authenticate, run: gh auth login -h github.com", file=sys.stderr)
        print("  - To forget about this account, run: gh auth logout -h github.com -u human", file=sys.stderr)
        sys.exit(1)
    print("github.com")
    print("  ✓ Logged in to github.com account human (keyring)")
    print("  - Active account: true")
    print("  - Token: gho_************************************")
    print("  - Token scopes: 'gist', 'project', 'read:org', 'repo', 'workflow'")
elif args[:2] in (["auth", "login"], ["auth", "logout"], ["auth", "refresh"], ["auth", "setup-git"]):
    sys.exit(0)
elif args[:2] == ["auth", "token"]:
    if source or keyring_invalid:
        print("token is invalid", file=sys.stderr)
        sys.exit(1)
    print("gho_offline_token")
elif args[:2] == ["repo", "view"]:
    print(json.dumps({"isPrivate": True}))
elif args[:2] == ["api", "user"]:
    if source or keyring_invalid:
        print("HTTP 401: Bad credentials (https://api.github.com/user)", file=sys.stderr)
        sys.exit(1)
    print(json.dumps({"login": "human", "type": "User"}))
else:
    print("[]")
'''


@pytest.fixture
def env_machine(machine, monkeypatch):  # noqa: F811
    fake = Path(os.environ["PATH"].split(os.pathsep)[0]) / "gh"
    fake.write_text(f"#!{sys.executable}\n{FAKE_GH}", encoding="utf-8")
    fake.chmod(0o755)
    monkeypatch.delenv("FAKE_GH_KEYRING_INVALID", raising=False)
    return machine


def run_init(machine, capsys, *extra) -> tuple[int, str]:
    capsys.readouterr()
    try:
        code = cli.main(["init", "--repo-root", str(machine.root), *extra, *INIT_FLAGS])
    except SystemExit as exc:
        code = exc.code
    captured = capsys.readouterr()
    return code, captured.out + captured.err


def lines(output: str) -> list[str]:
    return [line.strip() for line in output.splitlines() if line.strip()]


def first_index(shown: list[str], needle: str) -> int | None:
    return next((i for i, line in enumerate(shown) if needle in line), None)


def assert_no_secret(output: str) -> None:
    for secret in (GH_SECRET, GITHUB_SECRET):
        assert secret not in output, f"The token value is never printed:\n{output}"
        assert secret[8:] not in output, f"No part of the token value is printed:\n{output}"


def assert_no_credential_change(machine) -> None:
    gh_args = [call["args"] for call in machine.calls() if call["tool"] == "gh"]
    for args in gh_args:
        assert args[:2] not in (["auth", "login"], ["auth", "logout"], ["auth", "refresh"]), \
            f"init runs no interactive login or credential change: {gh_args}"


def assert_env_guidance(output: str, variable: str, inactive: str | None, language: str) -> None:
    shown = lines(output)
    assert "Traceback" not in output
    named = first_index(shown, variable)
    assert named is not None, f"The guidance names the active {variable}:\n{output}"
    pattern = UNSET_OR_REPLACE_EN if language == "en" else UNSET_OR_REPLACE_KO
    variable_lines = [line for line in shown if variable in line]
    assert any(pattern.search(line) for line in variable_lines), \
        f"The guidance says to unset or replace {variable}:\n{output}"
    login = first_index(shown, "gh auth login")
    if login is not None:
        assert named <= login, f"{variable} is dealt with before gh auth login:\n{output}"
    assert not SAVED_LOGIN.search(output), f"An env-token failure is not a saved-login token:\n{output}"
    if inactive is not None:
        assert not any(inactive in line and ("invalid" in line.lower() or "잘못" in line or "만료" in line)
                       for line in shown), f"The inactive {inactive} is not blamed:\n{output}"
    if language == "ko":
        assert any(HANGUL.search(line) for line in variable_lines), \
            f"The {variable} explanation is in Korean under language ko:\n{output}"
    else:
        assert not HANGUL.search(output), f"English only under --language en:\n{output}"


@pytest.mark.parametrize("language", ["en", "ko"])
def test_invalid_gh_token_names_gh_token_without_its_value(env_machine, monkeypatch, capsys, language):
    monkeypatch.setenv("GH_TOKEN", GH_SECRET)

    code, output = run_init(env_machine, capsys, "--language", language)

    assert code == 1, f"fa init exits with code 1:\n{output}"
    assert_env_guidance(output, "GH_TOKEN", None, language)
    assert "GITHUB_TOKEN" not in output, f"Only the active source is named:\n{output}"
    assert_no_secret(output)
    assert not (env_machine.root / ".foragent.yml").exists()


@pytest.mark.parametrize("language", ["en", "ko"])
def test_invalid_github_token_alone_names_github_token(env_machine, monkeypatch, capsys, language):
    monkeypatch.setenv("GITHUB_TOKEN", GITHUB_SECRET)

    code, output = run_init(env_machine, capsys, "--language", language)

    assert code == 1, f"fa init exits with code 1:\n{output}"
    assert_env_guidance(output, "GITHUB_TOKEN", None, language)
    assert not re.search(r"(?<![A-Z])GH_TOKEN", output), f"GH_TOKEN is not set and not named:\n{output}"
    assert_no_secret(output)


@pytest.mark.parametrize("language", ["en", "ko"])
def test_both_tokens_set_names_the_source_gh_selected(env_machine, monkeypatch, capsys, language):
    monkeypatch.setenv("GH_TOKEN", GH_SECRET)
    monkeypatch.setenv("GITHUB_TOKEN", GITHUB_SECRET)

    code, output = run_init(env_machine, capsys, "--language", language)

    assert code == 1, f"fa init exits with code 1:\n{output}"
    assert_env_guidance(output, "GH_TOKEN", "GITHUB_TOKEN", language)
    assert_no_secret(output)


def test_reporting_env_token_failure_changes_no_credentials(env_machine, monkeypatch, capsys):
    monkeypatch.setenv("GH_TOKEN", GH_SECRET)
    monkeypatch.setenv("GITHUB_TOKEN", GITHUB_SECRET)

    code, output = run_init(env_machine, capsys, "--language", "en")

    assert code == 1, output
    assert_env_guidance(output, "GH_TOKEN", "GITHUB_TOKEN", "en")
    assert os.environ.get("GH_TOKEN") == GH_SECRET and os.environ.get("GITHUB_TOKEN") == GITHUB_SECRET, \
        "init does not unset or replace the environment tokens"
    assert_no_credential_change(env_machine)
    gh_calls = [call for call in env_machine.calls() if call["tool"] == "gh"]
    assert gh_calls and all(call["env"] == {"GH_TOKEN": GH_SECRET, "GITHUB_TOKEN": GITHUB_SECRET}
                            for call in gh_calls), "gh sees the person's environment unchanged"
    assert not (env_machine.root / ".foragent.yml").exists()


@pytest.mark.parametrize("language", ["en", "ko"])
def test_saved_login_recovery_only_when_no_env_token_failed(env_machine, monkeypatch, capsys, language):
    """The same invalid saved login is explained two ways, depending on the failing source.

    First an invalid GITHUB_TOKEN masks the (also invalid) saved login: the guidance must blame
    the variable. Then, with the variable gone, the saved-login recovery must stay accurate.
    """
    monkeypatch.setenv("FAKE_GH_KEYRING_INVALID", "1")
    monkeypatch.setenv("GITHUB_TOKEN", GITHUB_SECRET)

    code, output = run_init(env_machine, capsys, "--language", language)

    assert code == 1, f"fa init exits with code 1:\n{output}"
    assert_env_guidance(output, "GITHUB_TOKEN", None, language)
    assert_no_secret(output)
    assert_no_credential_change(env_machine)

    monkeypatch.delenv("GITHUB_TOKEN")
    code, output = run_init(env_machine, capsys, "--language", language)

    assert code == 1, f"fa init exits with code 1:\n{output}"
    assert "Traceback" not in output
    assert "gh auth login" in output, f"The saved-login recovery is gh auth login:\n{output}"
    assert SAVED_LOGIN.search(output), f"The failure is described as the saved login:\n{output}"
    assert "GH_TOKEN" not in output and "GITHUB_TOKEN" not in output, \
        f"No environment variable is blamed when none supplied the credential:\n{output}"
    assert_no_credential_change(env_machine)
