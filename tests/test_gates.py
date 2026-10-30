"""Boundary cases of the machine gate list (#366); fake launchctl, temporary HOME."""

from datetime import UTC, datetime, timedelta
import json
import plistlib
import subprocess

import pytest
import yaml

from foragent import gates, launchd

NOW = datetime(2026, 10, 27, 8, tzinfo=UTC)


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "Library/LaunchAgents").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    return home


def launchctl(monkeypatch, codes):
    def call(argv):
        label = argv[1].rsplit("/", 1)[1]
        code = codes.get(label, 113)
        if isinstance(code, Exception):
            raise code
        return subprocess.CompletedProcess(argv, code, stdout="", stderr="")

    monkeypatch.setattr(launchd, "_run_launchctl", call)


def plist(home, label, root, interval=None):
    data = {"Label": label, "ProgramArguments": ["fa", "gate", "tick", "--repo-root", str(root)]}
    if interval is not None:
        data["StartInterval"] = interval
    path = home / "Library/LaunchAgents" / f"{label}.plist"
    path.write_bytes(plistlib.dumps(data))
    return path


def configured(root, repo):
    root.mkdir(parents=True, exist_ok=True)
    (root / ".foragent.yml").write_text(yaml.safe_dump({"repo": repo}), encoding="utf-8")


def tick(home, repo, at):
    root = home / ".local/share/foragent" / repo.replace("/", "__")
    root.mkdir(parents=True, exist_ok=True)
    (root / "gate-heartbeat.json").write_text(json.dumps({"at": at.isoformat()}), encoding="utf-8")
    return root


def collect(home):
    return {row["repo"]: row for row in gates.collect(NOW, home=home, data_root=home / ".local/share/foragent")}


def test_unreadable_plist_is_listed_by_path_as_unknown(home, monkeypatch):
    launchctl(monkeypatch, {})
    path = home / "Library/LaunchAgents/dev.foragent.broken.plist"
    path.write_bytes(b"not a plist")
    row = collect(home)[str(path)]
    assert (row["loaded"], row["scheduler"], row["stale"], row["last_tick"]) == (False, "unknown", False, None)


def test_plist_without_repo_key_or_remote_is_listed_by_root(home, tmp_path, monkeypatch):
    launchctl(monkeypatch, {"dev.foragent.plain": 0})
    root = tmp_path / "plain"
    root.mkdir()
    plist(home, "dev.foragent.plain", root)
    row = collect(home)[str(root.resolve())]
    assert row["loaded"] and row["stale"] and row["last_tick"] is None


def test_own_interval_decides_staleness(home, tmp_path, monkeypatch):
    launchctl(monkeypatch, {"dev.foragent.slow": 0, "dev.foragent.fast": 0})
    for name, interval in (("slow", 3600), ("fast", None)):
        configured(tmp_path / name, f"owner/{name}")
        plist(home, f"dev.foragent.{name}", tmp_path / name, interval)
        tick(home, f"owner/{name}", NOW - timedelta(minutes=20))
    rows = collect(home)
    assert rows["owner/slow"]["stale"] is False and rows["owner/slow"]["interval_seconds"] == 3600
    assert rows["owner/fast"]["stale"] is True and rows["owner/fast"]["interval_seconds"] == 300


def test_unknown_scheduler_state_is_not_loaded_and_not_stale(home, tmp_path, monkeypatch):
    launchctl(monkeypatch, {"dev.foragent.odd": 5})
    configured(tmp_path / "odd", "owner/odd")
    plist(home, "dev.foragent.odd", tmp_path / "odd")
    row = collect(home)["owner/odd"]
    assert (row["loaded"], row["scheduler"], row["stale"]) == (False, "unknown", False)
    assert "unknown" in "\n".join(gates.human_lines([row], NOW, "en"))


def test_state_only_repo_name_prefers_newest_record(home, monkeypatch):
    launchctl(monkeypatch, {})
    root = tick(home, "owner/folder", NOW)
    (root / "runs.jsonl").write_text(
        json.dumps({"number": 1, "url": "https://github.com/old/name/pull/1"}) + "\n"
        + json.dumps({"number": 2, "url": "https://github.com/new/name/pull/2"}) + "\n", encoding="utf-8")
    plain = tick(home, "owner/plain", NOW)
    rows = collect(home)
    assert rows["new/name"]["state_root"] == str(root.resolve())
    assert rows["owner/plain"]["state_root"] == str(plain.resolve())
    assert rows["owner/plain"]["loaded"] is False and rows["owner/plain"]["scheduler"] == "not loaded"


def test_plist_and_state_with_different_roots_are_two_rows(home, tmp_path, monkeypatch):
    launchctl(monkeypatch, {"dev.foragent.a": 0})
    configured(tmp_path / "a", "owner/a")
    plist(home, "dev.foragent.a", tmp_path / "a")
    tick(home, "owner/a", NOW)
    tick(home, "owner/b", NOW)
    rows = collect(home)
    assert sorted(rows) == ["owner/a", "owner/b"]
    assert rows["owner/a"]["loaded"] and not rows["owner/b"]["loaded"]


def test_no_gates_prints_one_line():
    assert gates.human_lines([], NOW, "en") == ["No Foragent gates are installed on this machine."]


def test_newest_timed_record_names_the_repo_across_runs_and_issues(home, monkeypatch):
    launchctl(monkeypatch, {})
    root = tick(home, "owner/folder", NOW)
    (root / "runs.jsonl").write_text(json.dumps({
        "number": 1, "url": "https://github.com/old/run/pull/1",
        "started_at": "2026-10-20T08:00:00+00:00", "finished_at": "2026-10-20T09:00:00+00:00",
    }) + "\n", encoding="utf-8")
    (root / "issues.json").write_text(json.dumps({"refreshed_at": None, "issues": {
        # A later observation must win over an earlier one regardless of its other facts.
        "1": {"number": 1, "url": "https://github.com/older/issue/issues/1",
              "observed": {"title": "zzz", "updated_at": "2026-10-21T08:00:00+00:00"}},
        "2": {"number": 2, "url": "https://github.com/new/issue/issues/2",
              "observed": {"title": "aaa", "updated_at": "2026-10-22T08:00:00+00:00"}},
    }}), encoding="utf-8")
    assert "new/issue" in collect(home)


def test_newest_run_record_wins_over_older_issue_record(home, monkeypatch):
    launchctl(monkeypatch, {})
    root = tick(home, "owner/folder", NOW)
    (root / "runs.jsonl").write_text(json.dumps({
        "number": 1, "url": "https://github.com/new/run/pull/1", "finished_at": "2026-10-23T08:00:00+00:00",
    }) + "\n", encoding="utf-8")
    (root / "issues.json").write_text(json.dumps({"refreshed_at": None, "issues": {
        "1": {"number": 1, "url": "https://github.com/old/issue/issues/1", "updated_at": "2026-10-21T08:00:00+00:00"},
    }}), encoding="utf-8")
    assert "new/run" in collect(home)


def lane_frame(rows, width):
    from types import SimpleNamespace

    from foragent.config import Config
    from foragent.watch_sections import gates as section

    snapshot = SimpleNamespace(status={"installed_gates": rows}, captured_at=NOW)
    lines = section.render(snapshot, Config(language="en"), None, width=width, now=NOW)
    return ["".join(value for value, style in line) for line in lines]


def gate_row(repo, **values):
    return {"repo": repo, "last_tick": (NOW - timedelta(minutes=20)).isoformat(), "running_runs": 0,
            "stale": False, "current": False, **values}


def test_lane_section_shows_iso_time_beside_relative_age():
    lines = lane_frame([gate_row("owner/a", stale=True, running_runs=2), gate_row("owner/b", last_tick=None)], 240)
    assert lines == ["Other gates: owner/a 2026-10-27T07:40Z (20 min ago) ⚠ 2 running · owner/b never"]


def test_lane_section_wraps_between_gates_so_none_is_clipped():
    from foragent import watch

    rows = [gate_row(f"owner/gate-number-{index}") for index in range(6)] + [gate_row("owner/me", current=True)]
    lines = lane_frame(rows, 120)
    assert len(lines) > 1 and all(watch._cells(line) <= 120 for line in lines)
    text = "\n".join(lines)
    assert all(f"owner/gate-number-{index} 2026-10-27T07:40Z" in text for index in range(6))
    assert "owner/me" not in text
    assert lane_frame([gate_row("owner/" + "x" * 300)], 60) and all(
        watch._cells(line) <= 60 for line in lane_frame([gate_row("owner/" + "x" * 300)], 60))


def test_newer_current_run_record_wins_over_older_history(home, monkeypatch):
    launchctl(monkeypatch, {})
    root = tick(home, "owner/folder", NOW)
    (root / "runs.jsonl").write_text(json.dumps({
        "number": 1, "repo": "old/name", "finished_at": "2026-10-20T09:00:00+00:00",
    }) + "\n", encoding="utf-8")
    (root / "runs").mkdir()
    (root / "runs/2.json").write_text(json.dumps({
        "number": 2, "repo": "new/name", "status": "running",
        "started_at": "2026-10-21T08:00:00+00:00", "last_progress_at": "2026-10-21T09:00:00+00:00",
    }), encoding="utf-8")
    assert "new/name" in collect(home) and "old/name" not in collect(home)


def test_merged_pr_record_names_the_repo_when_runs_and_issues_do_not(home, monkeypatch):
    launchctl(monkeypatch, {})
    root = tick(home, "owner/folder", NOW)
    (root / "issues.json").write_text(json.dumps({"refreshed_at": None, "issues": {
        "1": {"number": 1, "updated_at": "2026-10-21T08:00:00+00:00"}}}), encoding="utf-8")
    (root / "merged_prs.json").write_text(json.dumps({"watermark": "2026-10-22T00:00:00+00:00", "prs": [
        {"number": 7, "head": "h", "base": "main", "merged_at": "2026-10-21T08:00:00Z",
         "url": "https://github.com/real/name/pull/7"}]}), encoding="utf-8")
    assert "real/name" in collect(home) and "owner/folder" not in collect(home)


def test_unreadable_record_files_fall_back_to_the_folder_name(home, monkeypatch):
    launchctl(monkeypatch, {})
    root = tick(home, "owner/folder", NOW)
    (root / "runs").mkdir()
    (root / "runs/1.json").write_text("{broken", encoding="utf-8")
    (root / "merged_prs.json").write_text(json.dumps({"prs": "not a list"}), encoding="utf-8")
    (root / "issues.json").write_text("[]", encoding="utf-8")
    assert "owner/folder" in collect(home)


@pytest.mark.parametrize("language, words", [("en", "launchd did not load the job"),
                                             ("ko", "launchd가 작업을 불러오지 않았어요")])
def test_pending_reload_without_error_text_still_shows_failure_and_recovery_command(language, words):
    from datetime import UTC, datetime

    row = {"repo": "o/r", "repo_root": "/work/my repo", "last_tick": None, "running_runs": 0,
           "scheduler": "not loaded", "stale": False,
           "reload": {"at": "2026-10-31T09:00:00+00:00", "error": None, "attempts": 1,
                      "recovered": False, "loaded": False, "pending": True}}
    human = "\n".join(gates.human_lines([row], datetime.now(UTC), language))
    assert words in human and "fa gate install --repo-root '/work/my repo'" in human
    row["reload"] = {**row["reload"], "loaded": True, "pending": False}
    assert "fa gate install" not in "\n".join(gates.human_lines([row], datetime.now(UTC), language)), (
        "A clean reload shows no failure")
