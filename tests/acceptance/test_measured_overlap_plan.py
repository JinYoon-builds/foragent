"""Order acceptance: an agent's evidence-backed file plan controls admission."""

from dataclasses import replace
from datetime import timedelta

from foragent import cli
from foragent.scheduler import tick
from foragent.state import StateStore
from test_lane_clone import _lane_clone_issue
from test_lane_width_by_overlap import _wide_lane
from test_scheduler import ctx  # noqa: F401
from test_skills_autosync import cli_exit, isolated_home  # noqa: F401


PATH = "src/foragent/shared.py"
WHY = "20 recorded overlaps; one conflict; 60 resolution seconds total, 0 redo; five waiting"


def command(ctx, monkeypatch, capsys, *flags):
    monkeypatch.setattr(StateStore, "for_repo", classmethod(lambda cls, repo, **kw: ctx.store))
    (ctx.repo_root / ".foragent.yml").write_text("repo: owner/repo\nskill_libraries: []\n")
    capsys.readouterr()
    code = cli_exit(cli.main, ["orch", "plan", "--repo-root", str(ctx.repo_root), *flags])
    captured = capsys.readouterr()
    return code, captured.out + captured.err


def set_file_plan(ctx, monkeypatch, capsys, value="5", by="orchestrator"):
    expires = (ctx.now() + timedelta(hours=1)).isoformat()
    code, output = command(ctx, monkeypatch, capsys, "--overlap", f"{PATH}={value}",
                           "--why", WHY, "--by", by, "--expires", expires, "--json")
    assert code == 0, f"The planning agent needs a public file-overlap control: {output}"
    return output


def test_recorded_file_plan_allows_fifth_run_without_releasing_exclusive_claims(ctx, monkeypatch, capsys):
    starts = _wide_lane(ctx, 8)
    ctx.config = replace(ctx.config, overlap_per_file=3)
    # Persist the records the agent's explanation refers to, using the existing wire format.
    for number in range(100, 120):
        ctx.store.start_run(number, branch=f"history-{number}", worktree=ctx.store.worktree_path(number))
        ctx.store.update_run(number, overlap_records=[{
            "file": PATH, "issues": [90], "conflicted": number == 100,
            "resolution_seconds": 60 if number == 100 else 0, "redo_count": 0}])
        ctx.store.finish_run(number, outcome="merged")
    output = set_file_plan(ctx, monkeypatch, capsys)
    assert PATH in output and WHY in output, "The public plan retains the decision and its evidence"
    for number in range(1, 7):
        _lane_clone_issue(ctx, number, [PATH])
    _lane_clone_issue(ctx, 7, ["src/foragent/other.py"])
    exclusive = ctx.tracker.get_issue(7)
    ctx.tracker.update_issue(7, title=exclusive.title, body=exclusive.body.replace(
        "<!-- foragent:v1 -->\n", f"<!-- foragent:v1 -->\nexclusive:\n  - {PATH}\n"))
    report = tick(ctx)
    assert report.skipped is None
    assert starts == [1, 2, 3, 4, 5], report.actions
    assert "ready" in ctx.tracker.get_issue(6).labels
    assert "ready" in ctx.tracker.get_issue(7).labels, "A file plan never releases exclusive reservations"
    events = (ctx.store.root / "orch-events.jsonl").read_text()
    assert PATH in events and WHY in events and "orchestrator" in events


def test_human_plan_is_protected_and_expiry_restores_the_configured_fallback(ctx, monkeypatch, capsys):
    starts = _wide_lane(ctx, 8)
    ctx.config = replace(ctx.config, overlap_per_file=3)
    set_file_plan(ctx, monkeypatch, capsys, value="1", by="human")
    before = (ctx.store.root / "plan.json").read_bytes()
    code, output = command(ctx, monkeypatch, capsys, "--overlap", f"{PATH}=5", "--why", WHY,
                           "--by", "orchestrator", "--json")
    assert code != 0 and "human" in output.lower()
    assert (ctx.store.root / "plan.json").read_bytes() == before
    for number in range(1, 5):
        _lane_clone_issue(ctx, number, [PATH])
    assert tick(ctx).skipped is None
    assert starts == [1]
    now = ctx.now()
    ctx.now = lambda: now + timedelta(hours=2)
    assert tick(ctx).skipped is None
    assert starts == [1, 2, 3], "Expired file plans fall back to configured overlap_per_file"


def test_fixed_config_override_wins_over_recorded_file_plan(ctx, monkeypatch, capsys):
    starts = _wide_lane(ctx, 8)
    # A product setting, not a new automatic cost classifier.
    set_file_plan(ctx, monkeypatch, capsys)
    from foragent.config import load_config

    config_file = ctx.repo_root / ".foragent.yml"
    config_file.write_text(config_file.read_text() + "overlap_mode: fixed\noverlap_per_file: 1\n")
    loaded = load_config(ctx.repo_root)
    ctx.config = replace(loaded, lanes=ctx.config.lanes)
    for number in range(1, 4):
        _lane_clone_issue(ctx, number, [PATH])
    assert tick(ctx).skipped is None
    assert starts == [1]


def test_file_expiry_cannot_expire_unrelated_human_plan_items(ctx, monkeypatch, capsys):
    later = (ctx.now() + timedelta(days=1)).isoformat()
    code, output = command(ctx, monkeypatch, capsys, "--width", "main=2",
                           "--expires", later, "--by", "human", "--json")
    assert code == 0, output
    import json

    before = json.loads(output)
    set_file_plan(ctx, monkeypatch, capsys)
    code, output = command(ctx, monkeypatch, capsys, "--json")
    assert code == 0, output
    after = json.loads(output)
    for key in ("queues", "paused", "width", "expires_at"):
        assert after[key] == before[key], "A file edit cannot change unrelated human policy"


def test_equivalent_file_spelling_cannot_bypass_a_human_limit(ctx, monkeypatch, capsys):
    set_file_plan(ctx, monkeypatch, capsys, value="1", by="human")
    before = (ctx.store.root / "plan.json").read_bytes()
    for path in ("./" + PATH, "src/foragent/../foragent/shared.py", "../outside.py"):
        code, output = command(ctx, monkeypatch, capsys, "--overlap", f"{path}=5",
                               "--why", WHY, "--by", "orchestrator", "--json")
        assert code != 0, output
        assert (ctx.store.root / "plan.json").read_bytes() == before
