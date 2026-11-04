"""Setup readers share scope decisions without changing machine state."""

import json
import shutil
import subprocess

import pytest

from foragent import cli, gh, setup as checklist_setup
from foragent.init import _run_gh
from foragent.tracker import TrackerError
from test_init import setup
from conftest import GitHistory, git_command


FIX = "gh auth refresh -h github.com -s project"


@pytest.fixture(autouse=True)
def awake_machine(monkeypatch):
    """Scope tests must not depend on the host's power settings or blockers."""
    monkeypatch.setattr(checklist_setup.sys, "platform", "darwin")
    run = subprocess.run

    def probe(argv, *args, **kwargs):
        if argv == ["pmset", "-g"]:
            return subprocess.CompletedProcess(argv, 0, stdout=" sleep 0\n", stderr="")
        return run(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", probe)


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
    item = checklist_setup.gh_project_scope(account(scopes))
    assert item == ({"id": "gh-project-scope", "ok": True} if ok else
                    {"id": "gh-project-scope", "ok": False, "fix": FIX})


@pytest.mark.parametrize("active_first", [False, True])
@pytest.mark.parametrize("active_scopes,ok", [("'repo'", False), ("'project'", True), (None, True)])
def test_only_active_github_com_account_counts(active_first, active_scopes, ok):
    active = account(active_scopes)
    inactive = account("'project'" if not ok else "'repo'", active=False, name="other").split("\n", 1)[1]
    status = (active + inactive if active_first else account("'project'", active=False) + active.split("\n", 1)[1])
    status += account("'project'", host="work.example.com")
    assert checklist_setup.gh_project_scope(status)["ok"] is ok


@pytest.mark.parametrize("status", ["", "not logged in", account("'repo'", host="work.example.com")])
def test_unknown_github_scopes_pass(status):
    assert checklist_setup.gh_project_scope(status)["ok"] is True


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
    items = json.loads(capsys.readouterr().out)["items"]
    by_id = {item["id"]: item for item in items}
    assert by_id["gh-project-scope"] == {"id": "gh-project-scope", "ok": False, "fix": FIX}
    assert not by_id["repository-linked"]["ok"]
    assert checklist_setup.gh_project_scope(_run_gh(["auth", "status"]))["fix"] == FIX
    assert calls == [["auth", "status"], ["auth", "status"]]


@pytest.mark.parametrize("failure", [FileNotFoundError("gh"), subprocess.TimeoutExpired("gh", 10), None])
def test_doctor_scopes_wait_when_auth_cannot_be_read(tmp_path, monkeypatch, capsys, failure):
    def run(*args, **kwargs):
        if failure is not None:
            raise failure
        return subprocess.CompletedProcess(["gh", "auth", "status"], 1, account("'repo'"), "failed")

    monkeypatch.setattr(gh, "run", run)
    assert cli.main(["doctor", "--setup", "--json", "--repo-root", str(tmp_path)]) == 1
    items = json.loads(capsys.readouterr().out)["items"]
    scope = next(item for item in items if item["id"] == "gh-project-scope")
    assert scope["ok"] is False
    assert "gh auth" not in scope["fix"]
    assert ("gh-installed" if isinstance(failure, FileNotFoundError) else "gh-logged-in") in scope["fix"]


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
        checklist_setup.project_scope,
        lambda status: {"id": "later", "ok": False, "fix": "last fix"},
    ))
    monkeypatch.setattr(gh, "run", lambda *a, **kw: subprocess.CompletedProcess([], 0, "", ""))
    assert cli.main(["doctor", "--setup", "--repo-root", str(setup.root)]) == 1
    output = capsys.readouterr().out
    assert output.index("first fix") < output.index("PASS gh-project-scope") < output.index("last fix")
    with pytest.raises(TrackerError) as error:
        setup.init()
    assert str(error.value).index("first fix") < str(error.value).index("last fix")


@pytest.mark.parametrize("language", ["en", "ko"])
@pytest.mark.parametrize("json_output", [False, True])
def test_doctor_sleep_advice_uses_repo_language(tmp_path, monkeypatch, capsys, language, json_output):
    from foragent.i18n import text

    run = subprocess.run

    def power_probe(argv, *args, **kwargs):
        if argv[:2] == ["pmset", "-g"]:
            output = " sleep 1\n" if argv == ["pmset", "-g"] else "Listed by owning process:\n"
            return subprocess.CompletedProcess(argv, 0, stdout=output, stderr="")
        return run(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", power_probe)
    monkeypatch.setattr(gh, "run", lambda *a, **kw: subprocess.CompletedProcess([], 0, account("'project'"), ""))
    (tmp_path / ".foragent.yml").write_text(f"language: {language}\n")
    args = ["doctor", "--setup", "--repo-root", str(tmp_path)]
    assert cli.main([*args, *(["--json"] if json_output else [])]) == 1
    output = capsys.readouterr().out
    expected = text("setup_no_idle_sleep_fix", language)
    if json_output:
        items = json.loads(output)["items"]
        item = next(item for item in items if item["id"] == "no-idle-sleep")
        assert item == {"id": "no-idle-sleep", "ok": False, "advice": True, "fix": expected}
    else:
        assert expected in output


@pytest.mark.parametrize("state", ["plain", "unlinked", "linked", "other-host"])
def test_repository_checklist_and_init_share_fixes_without_writes(tmp_path, monkeypatch, capsys, state):
    from foragent.git import GitError
    from foragent.init import init_repo

    root = tmp_path / "project"
    root.mkdir()
    (root / "notes.txt").write_text("Keep my notes\n")
    if state != "plain":
        git_command(root, "init", "--initial-branch=main")
    if state in {"linked", "other-host"}:
        host = "github.com" if state == "linked" else "gitlab.com"
        git_command(root, "remote", "add", "origin", f"https://{host}/owner/repo.git")
    # --repo-root must win over cwd, which is a different plain folder.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(gh, "run", lambda *a, **kw: subprocess.CompletedProcess([], 0, account("'project'"), ""))

    def files():
        return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}

    before = files()
    code = cli.main(["doctor", "--setup", "--json", "--repo-root", str(root)])
    items = json.loads(capsys.readouterr().out)["items"]
    ids = [item["id"] for item in items]
    assert ids.index("gh-project-scope") < ids.index("repository-linked")
    item = next(item for item in items if item["id"] == "repository-linked")
    assert item["ok"] is (state == "linked")
    assert code == (0 if state == "linked" else 1)
    if state == "linked":
        assert "fix" not in item
    else:
        with pytest.raises(GitError) as error:
            init_repo(root, gh_run=lambda *a, **kw: pytest.fail("repository preflight precedes auth"))
        assert str(error.value) == item["fix"]
        if state == "other-host":
            assert "git remote set-url origin https://github.com/<owner>/<repo>.git" in item["fix"]
            assert "git remote add origin" not in item["fix"]
        else:
            for command in ("gh repo create <owner>/<repo> --private --source .",
                            "gh repo create <owner>/<repo> --public --source .",
                            "git remote add origin https://github.com/<owner>/<repo>.git"):
                assert command in item["fix"]
            assert ("  git init\n" in item["fix"]) is (state == "plain")
    assert files() == before
    assert (root / ".git").exists() is (state != "plain")


def test_repository_checklist_accepts_nested_and_linked_worktrees(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    git_command(root, "init", "--initial-branch=main")
    git_command(root, "remote", "add", "origin", "git@github.com:owner/repo.git")
    GitHistory(root, root).update({})
    nested = root / "src"
    nested.mkdir()
    linked = tmp_path / "linked"
    git_command(root, "worktree", "add", "-b", "feature", str(linked))
    for folder in (nested, linked):
        assert checklist_setup.repository_linked(folder) == {"id": "repository-linked", "ok": True}


@pytest.mark.parametrize("state,first_failure", [
    ("missing", "gh-installed"),
    ("logged-out", "gh-logged-in"),
    ("needs-scope", "gh-project-scope"),
    ("ready", None),
])
def test_doctor_reports_ordered_prerequisites_in_linked_repo(setup, monkeypatch, capsys, state, first_failure):
    git_command(setup.root, "remote", "set-url", "origin", "https://github.com/owner/repo.git")
    original_which = shutil.which
    monkeypatch.setattr(shutil, "which", lambda tool, *a, **kw:
                        None if tool == "gh" and state == "missing" else
                        f"/test/bin/{tool}" if tool in {"git", "gh", "uv"} else original_which(tool, *a, **kw))
    calls = []

    def read_auth(args, **kwargs):
        calls.append(args)
        assert args == ["auth", "status"]
        if state == "missing":
            raise FileNotFoundError("gh")
        return subprocess.CompletedProcess(["gh", *args], 1 if state == "logged-out" else 0,
                                           account("'project'" if state == "ready" else "'repo'"), "not logged in")

    monkeypatch.setattr(gh, "run", read_auth)
    before = {str(p.relative_to(setup.root)): p.read_bytes() for p in setup.root.rglob("*") if p.is_file()}
    code = cli.main(["doctor", "--setup", "--json", "--repo-root", str(setup.root)])
    items = json.loads(capsys.readouterr().out)["items"]
    ids = [item["id"] for item in items]
    required = ["git-installed", "gh-installed", "uv-installed", "gh-logged-in", "gh-project-scope", "repository-linked"]
    assert [ids.index(name) for name in required] == sorted(ids.index(name) for name in required)
    failed = [item for item in items if not item["ok"]]
    assert code == (1 if first_failure else 0)
    assert (failed[0]["id"] if failed else None) == first_failure
    by_id = {item["id"]: item for item in items}
    if state == "missing":
        assert "gh" in failed[0]["fix"] and ("install" in failed[0]["fix"] or "https://" in failed[0]["fix"])
        for name in ("gh-logged-in", "gh-project-scope"):
            assert by_id[name]["ok"] is False
            assert "gh-installed" in by_id[name]["fix"] and "gh auth" not in by_id[name]["fix"]
        assert calls == []
    else:
        assert calls == [["auth", "status"]]
        if state == "logged-out":
            assert by_id["gh-logged-in"]["fix"] == "gh auth login"
            assert by_id["gh-project-scope"]["ok"] is False
            assert "gh-logged-in" in by_id["gh-project-scope"]["fix"]
        elif state == "needs-scope":
            assert failed[0]["fix"] == FIX
    assert {str(p.relative_to(setup.root)): p.read_bytes() for p in setup.root.rglob("*") if p.is_file()} == before


@pytest.mark.parametrize("state", ["missing", "logged-out", "needs-scope"])
def test_init_reports_shared_prerequisite_fixes_before_setup(setup, monkeypatch, capsys, state):
    git_command(setup.root, "remote", "set-url", "origin", "https://github.com/owner/repo.git")
    original_which = shutil.which
    monkeypatch.setattr(shutil, "which", lambda tool, *a, **kw:
                        None if tool == "gh" and state == "missing" else
                        f"/test/bin/{tool}" if tool in {"git", "gh", "uv"} else original_which(tool, *a, **kw))

    def read_auth(args, **kwargs):
        if state == "missing":
            raise FileNotFoundError("gh")
        if state == "logged-out":
            raise subprocess.CalledProcessError(1, ["gh", *args], stderr="not logged in")
        return account("'repo'")

    monkeypatch.setattr(gh, "run", lambda args, **kw: subprocess.CompletedProcess(["gh", *args], 0, read_auth(args), ""))
    monkeypatch.setattr(setup, "run_gh", read_auth)
    assert cli.main(["doctor", "--setup", "--json", "--repo-root", str(setup.root)]) == 1
    failing = [item for item in json.loads(capsys.readouterr().out)["items"] if not item["ok"]]
    expected = {
        "missing": ["gh-installed", "gh-logged-in", "gh-project-scope"],
        "logged-out": ["gh-logged-in", "gh-project-scope"],
        "needs-scope": ["gh-project-scope"],
    }
    assert [item["id"] for item in failing] == expected[state]
    with pytest.raises(TrackerError) as error:
        setup.init()
    for item in failing:
        assert item["fix"] in str(error.value)
    assert not (setup.root / ".foragent.yml").exists()
    assert not setup.tracker.labels and not setup.installs
    assert not any("commit" in args or "push" in args for args in setup.git_calls)
