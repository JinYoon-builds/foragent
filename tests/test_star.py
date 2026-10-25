import json
import subprocess

import pytest

from foragent import capacity, star


@pytest.fixture
def machine(tmp_path, monkeypatch):
    monkeypatch.setattr(capacity, "machine_root", lambda: tmp_path)
    return tmp_path


def merges(machine, repo, numbers, outcome="merged"):
    path = machine / repo / "runs.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for number in numbers:
            handle.write(json.dumps({"number": number, "outcome": outcome}) + "\n")


def fake_gh(monkeypatch, *, auth=0, put_fails=False):
    calls = []

    def run(argv, *args, **kwargs):
        calls.append(list(argv))
        if argv[:2] == ["gh", "auth"]:
            return subprocess.CompletedProcess(argv, auth, "", "")
        if put_fails:
            raise subprocess.CalledProcessError(1, argv, "", "HTTP 500")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(star.subprocess, "run", run)
    return calls


def test_local_merges_counts_distinct_issues_per_repository(machine):
    merges(machine, "a__b", [1, 1, 2])
    merges(machine, "c__d", [1])
    merges(machine, "c__d", [3], outcome="blocked")
    assert star.local_merges() == 3


def test_failed_star_is_not_recorded_and_the_question_returns(machine, monkeypatch):
    merges(machine, "a__b", range(10))
    calls = fake_gh(monkeypatch, put_fails=True)
    assert star.offer("en")
    assert star.answer("yes", "en") == star.text("star_failed", "en")
    assert not (machine / star.RECORD).exists()
    assert star.offer("en")
    assert calls.count(["gh", "api", "-X", "PUT", f"user/starred/{star.FORAGENT_REPO}"]) == 1


def test_star_answer_without_an_open_request_is_ignored(machine, monkeypatch):
    calls = fake_gh(monkeypatch)
    assert star.answer("yes", "ko") == star.text("star_not_asked", "ko")
    assert not (machine / star.RECORD).exists()
    assert ["gh", "api", "-X", "PUT", f"user/starred/{star.FORAGENT_REPO}"] not in calls


def test_logged_out_gh_shows_nothing(machine, monkeypatch):
    merges(machine, "a__b", range(10))
    fake_gh(monkeypatch, auth=1)
    assert star.offer("en") is None
    assert not (machine / star.RECORD).exists()


def test_star_with_json_is_rejected(tmp_path, capsys):
    from foragent import cli

    assert cli.main(["brief", "--star", "yes", "--json", "--repo-root", str(tmp_path)]) == 2
    assert "--json" in capsys.readouterr().err


def test_only_the_invocation_that_records_exposure_shows_the_question(machine, monkeypatch):
    merges(machine, "a__b", range(10))
    fake_gh(monkeypatch)
    # Another `fa brief` recorded exposure after this one passed the eligibility check.
    monkeypatch.setattr(star, "_eligible", lambda: True)
    (machine / star.RECORD).write_text('{"state": "shown"}\n', encoding="utf-8")
    assert star.offer("en") is None
    assert not list(machine.glob(".star-prompt-*"))


def test_yes_after_logout_is_ignored_and_keeps_the_shown_record(machine, monkeypatch):
    merges(machine, "a__b", range(10))
    fake_gh(monkeypatch)
    assert star.offer("en")
    calls = fake_gh(monkeypatch, auth=1)
    assert star.answer("yes", "en") == star.text("star_not_asked", "en")
    assert ["gh", "api", "-X", "PUT", f"user/starred/{star.FORAGENT_REPO}"] not in calls
    assert json.loads((machine / star.RECORD).read_text(encoding="utf-8"))["state"] == "shown"
    fake_gh(monkeypatch)
    assert star.offer("en") is None


def test_failed_yes_never_removes_a_final_answer_recorded_meanwhile(machine, monkeypatch):
    merges(machine, "a__b", range(10))
    fake_gh(monkeypatch)
    assert star.offer("en")
    final = {"state": "answered", "answer": "no"}

    def run(argv, *args, **kwargs):
        if argv[:2] == ["gh", "auth"]:
            return subprocess.CompletedProcess(argv, 0, "", "")
        (machine / star.RECORD).write_text(json.dumps(final), encoding="utf-8")
        raise subprocess.CalledProcessError(1, argv, "", "HTTP 500")

    monkeypatch.setattr(star.subprocess, "run", run)
    assert star.answer("yes", "en") == star.text("star_failed", "en")
    assert json.loads((machine / star.RECORD).read_text(encoding="utf-8")) == final
    assert star.offer("en") is None


def test_unconfigured_folder_asks_in_english(machine, monkeypatch, tmp_path, capsys):
    from foragent import cli

    merges(machine, "a__b", range(10))
    fake_gh(monkeypatch)
    folder = tmp_path / "plain"
    folder.mkdir()
    assert cli.main(["brief", "--repo-root", str(folder)]) == 0
    assert star.text("star_question", "en", repo=star.FORAGENT_REPO) in capsys.readouterr().out
    assert cli.main(["brief", "--json", "--repo-root", str(folder)]) == 0
    assert "Star" not in capsys.readouterr().out
