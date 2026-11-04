"""Setup readers share scope decisions without changing machine state."""

import json
import subprocess

import pytest

from foragent import cli, gh, setup as checklist_setup
from foragent.init import _run_gh
from foragent.tracker import TrackerError
from test_init import setup


FIX = "gh auth refresh -h github.com -s project"


def account(scopes=None, *, active=True, host="github.com", name="human"):
    return (f"{host}\n  ✓ Logged in to {host} account {name} (keyring)\n"
            f"  - Active account: {str(active).lower()}\n" +
            (f"  - Token scopes: {scopes}\n" if scopes is not None else ""))


@pytest.mark.parametrize("scopes,ok", [
    ("'repo', 'project', 'read:org'", True),
    ('"project", "repo"', True),
    ("'repo', 'read:project'", False),
    ("'repo', 'project:read'", False),
    ("'repo', 'workflow'", False),
    ("", False),
    (None, True),  # GH_TOKEN: no disclosed scope list.
])
def test_scope_is_an_exact_token(scopes, ok):
    item = checklist_setup.checklist(account(scopes))[0]
    assert item == ({"id": "gh-project-scope", "ok": True} if ok else
                    {"id": "gh-project-scope", "ok": False, "fix": FIX})


@pytest.mark.parametrize("active_first", [False, True])
@pytest.mark.parametrize("active_scopes,ok", [("'repo'", False), ("'project'", True), (None, True)])
def test_only_active_github_com_account_counts(active_first, active_scopes, ok):
    active = account(active_scopes)
    inactive = account("'project'" if not ok else "'repo'", active=False, name="other").split("\n", 1)[1]
    status = (active + inactive if active_first else account("'project'", active=False) + active.split("\n", 1)[1])
    status += account("'project'", host="work.example.com")
    assert checklist_setup.checklist(status)[0]["ok"] is ok


@pytest.mark.parametrize("status", ["", "not logged in", account("'repo'", host="work.example.com")])
def test_unknown_github_scopes_pass(status):
    assert checklist_setup.checklist(status)[0]["ok"] is True


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_doctor_and_init_read_both_auth_streams(tmp_path, monkeypatch, capsys, stream):
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(["gh", *args], 0, **{
            "stdout": account("'repo'") if stream == "stdout" else "",
            "stderr": account("'repo'") if stream == "stderr" else "",
        })

    monkeypatch.setattr(gh, "run", run)
    assert cli.main(["doctor", "--setup", "--json", "--repo-root", str(tmp_path)]) == 1
    assert json.loads(capsys.readouterr().out) == {"items": [{"id": "gh-project-scope", "ok": False, "fix": FIX}]}
    assert checklist_setup.checklist(_run_gh(["auth", "status"]))[0]["fix"] == FIX
    assert calls == [["auth", "status"], ["auth", "status"]]


@pytest.mark.parametrize("failure", [FileNotFoundError("gh"), subprocess.TimeoutExpired("gh", 10), None])
def test_doctor_scope_unknown_when_auth_cannot_be_read(tmp_path, monkeypatch, capsys, failure):
    def run(*args, **kwargs):
        if failure is not None:
            raise failure
        return subprocess.CompletedProcess(["gh", "auth", "status"], 1, account("'repo'"), "failed")

    monkeypatch.setattr(gh, "run", run)
    assert cli.main(["doctor", "--setup", "--json", "--repo-root", str(tmp_path)]) == 0
    assert json.loads(capsys.readouterr().out)["items"][0]["ok"] is True


def test_plain_doctor_never_reads_gh(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(gh, "run", lambda *a, **kw: pytest.fail("plain doctor must stay offline"))
    (tmp_path / ".foragent.yml").write_text("{}\n")
    assert cli.main(["doctor", "--repo-root", str(tmp_path)]) == 0
    assert "PASS" in capsys.readouterr().out


@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize("language", ["en", "ko"])
def test_init_stops_at_shared_checklist_before_setup(setup, monkeypatch, dry_run, language):
    original = setup.run_gh
    monkeypatch.setattr(setup, "run_gh", lambda args, input=None:
                        account("'repo'") if args == ["auth", "status"] else original(args, input))
    with pytest.raises(TrackerError, match=FIX):
        setup.init(dry_run=dry_run, language=language)
    assert not (setup.root / ".foragent.yml").exists()
    assert not setup.tracker.labels and not setup.installs
    assert not any("commit" in args or "push" in args for args in setup.git_calls)


def test_readers_use_the_same_ordered_list_for_later_items(setup, monkeypatch, capsys):
    monkeypatch.setattr(checklist_setup, "CHECKS", (
        lambda status: {"id": "earlier", "ok": False, "fix": "first fix"},
        checklist_setup.gh_project_scope,
        lambda status: {"id": "later", "ok": False, "fix": "last fix"},
    ))
    monkeypatch.setattr(gh, "run", lambda *a, **kw: subprocess.CompletedProcess([], 0, "", ""))
    assert cli.main(["doctor", "--setup", "--repo-root", str(setup.root)]) == 1
    output = capsys.readouterr().out
    assert output.index("first fix") < output.index("PASS gh-project-scope") < output.index("last fix")
    with pytest.raises(TrackerError) as error:
        setup.init()
    assert str(error.value).index("first fix") < str(error.value).index("last fix")
