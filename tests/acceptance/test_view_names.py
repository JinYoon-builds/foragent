"""Acceptance test for view-names. Locked by Foragent; do not edit."""


def test_lane_display_localized_main_custom_and_unknown_fallbacks():
    from foragent.config import Config
    from foragent.labels import lane_display

    config = Config(lanes={"api": {"paths": ["api/**"], "title": "서버", "emoji": "🔧"}})
    assert lane_display(config, "api", "ko") == "🔧 서버"
    assert lane_display(config, "api", "en") == "🔧 서버"
    assert lane_display(config, "main", "ko") == "🧰 공통"
    assert lane_display(config, "main", "en") == "🧰 Common"
    assert lane_display(config, "unconfigured", "ko") == "unconfigured"


def test_cli_add_stores_human_name_and_emoji(git_history, tmp_path, monkeypatch):
    import yaml

    from foragent.config import load_config
    from foragent.git import GitRepo
    from test_orch_review_handoff import bind_cli, make_case

    case = make_case(git_history, tmp_path, monkeypatch)
    main = bind_cli(case, monkeypatch)
    monkeypatch.setattr(GitRepo, "commit_paths", lambda *a, **kw: None)
    monkeypatch.setattr(GitRepo, "push_base", lambda *a, **kw: None)
    assert main(["lanes", "add", "api", "--paths", "api/**", "--title", "Server", "--emoji", "🔧"]) == 0
    lane = yaml.safe_load((case.root / ".foragent.yml").read_text())["lanes"]["api"]
    assert lane["title"] == "Server" and lane["emoji"] == "🔧"
    assert load_config(case.root, global_path=tmp_path / "absent.yml").lanes["api"]["title"] == "Server"


def test_status_and_inspect_show_display_name_and_json_keeps_slug(git_history, tmp_path, monkeypatch, capsys):
    import json

    from foragent.actions import flag
    from foragent.orch import escalate
    from foragent.reasons import Reason
    from foragent.scheduler import tick
    from test_orch_review_handoff import bind_cli, make_case, tick_context

    case = make_case(git_history, tmp_path, monkeypatch,
                     lanes={"api": {"paths": ["api/**"], "title": "Server", "emoji": "🔧"}})
    case.tracker.add_labels(1, ["lane:api"])
    case.store.update_run(1, lane="api")
    tick(tick_context(case, start_runs=False))
    main = bind_cli(case, monkeypatch)
    assert main(["gate", "status"]) == 0
    assert "🔧 Server" in capsys.readouterr().out
    assert main(["gate", "status", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    serialized = json.dumps(payload, ensure_ascii=False)
    assert '"api"' in serialized and '"lane_display": "🔧 Server"' in serialized
    flag(case.ctx, 1, Reason.SPEC_QUESTION, "Choose a locale")
    case.store.finish_run(1, outcome="spec-question")
    escalate(case.ctx, 1, "Choose a locale", "Reviewed the available locales")
    assert main(["inspect"]) == 0
    assert "🔧 Server" in capsys.readouterr().out
