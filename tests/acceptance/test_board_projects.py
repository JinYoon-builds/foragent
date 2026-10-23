"""Acceptance test for board-projects. Locked by Foragent; do not edit."""


def board_snapshot(tracker, board_id):
    """Read observable Projects v2 state through the offline GraphQL transport."""
    result = tracker.graphql(
        """query($id: ID!) {
          node(id: $id) { ... on ProjectV2 {
            id title
            fields(first: 100) { nodes { ... on ProjectV2SingleSelectField {
              name options { name }
            } } }
            items(first: 100) { nodes {
              content { ... on Issue { number } }
              fieldValues(first: 100) { nodes { ... on ProjectV2ItemFieldSingleSelectValue {
                name field { ... on ProjectV2SingleSelectField { name } }
              } } }
            } }
          } }
        }""", {"id": board_id},
    )
    return result.get("data", result)["node"]


def initialized_case(git_history, tmp_path, monkeypatch, *, no_board=False):
    import yaml

    from foragent import cli
    from foragent.init import init_repo
    from test_orch_review_handoff import bind_cli, make_case

    case = make_case(git_history, tmp_path, monkeypatch)
    main = bind_cli(case, monkeypatch)
    monkeypatch.setattr(case.git, "remote_slug", lambda: "owner/repo")
    # Setup publishing is recorded, never executed as commit/push subprocesses.
    monkeypatch.setattr(case.git, "commit_paths", lambda *a, **kw: None)
    monkeypatch.setattr(cli, "init_repo", lambda root, **kw: init_repo(
        root, git=case.git, tracker=case.tracker, gh_run=lambda *a, **kw: "human", **kw))
    argv = ["init", "--no-push", "--no-launchd", "--approver", "human"]
    if no_board:
        argv.append("--no-board")
    assert main(argv) == 0
    return case, main, argv, yaml.safe_load((case.root / ".foragent.yml").read_text())


def test_init_creates_single_select_board_once_and_persists_id(git_history, tmp_path, monkeypatch):
    import yaml

    case, main, argv, config = initialized_case(git_history, tmp_path, monkeypatch)
    assert config["board"]
    project = board_snapshot(case.tracker, config["board"])
    assert project["title"] == "Foragent: repo"
    fields = {field["name"]: [option["name"] for option in field["options"]]
              for field in project["fields"]["nodes"] if field}
    assert "Lane" in fields
    assert set(fields["Size"]) == {"s", "m", "l"}
    # D31 addendum: the board shows five grouped columns; test_board_status_groups owns the details.
    assert fields["Status"] == ["🗂 Backlog", "🌰 Up next", "🐿 In progress", "✋ Your turn", "📦 Done"]
    assert main(argv) == 0
    assert yaml.safe_load((case.root / ".foragent.yml").read_text())["board"] == config["board"]
    assert board_snapshot(case.tracker, config["board"])["fields"] == project["fields"]


def test_init_no_board_leaves_board_unconfigured(git_history, tmp_path, monkeypatch):
    case, main, argv, config = initialized_case(git_history, tmp_path, monkeypatch, no_board=True)
    assert not config.get("board")


def test_publish_adds_issues_with_lane_and_size(git_history, tmp_path, monkeypatch):
    from dataclasses import replace

    from foragent.order import publish
    from foragent.git import GitRepo

    case, main, argv, config = initialized_case(git_history, tmp_path, monkeypatch)
    # Restore the real public method: the injected Git runner imports the index offline.
    monkeypatch.setattr(case.git, "commit_paths", GitRepo.commit_paths.__get__(case.git))
    case.ctx.config = replace(case.config, board=config["board"], lanes={"api": {"paths": ["api/**"]}})
    path = "tests/acceptance/test_board_item.py"
    plan = {"id": "board-items", "title": "Board items", "summary": "A board item", "spec_markdown": "# Board\n",
            "issues": [{"key": "item", "title": "Board item", "goal": "Implement board item", "out_of_scope": "Other work",
                        "lane": "api", "size": "s", "done_when": [f"uv run pytest -q {path}"],
                        "acceptance_files": {path: "def test_item():\n    assert True\n"}}]}
    report = publish(plan, case.root, case.ctx.config, case.tracker, repo="owner/repo", git=case.git)
    child = next(issue for issue in case.tracker.list_open_issues() if issue.title == "Board item")
    project = board_snapshot(case.tracker, config["board"])
    item = next(item for item in project["items"]["nodes"] if item["content"]["number"] == child.number)
    values = {value["field"]["name"]: value["name"] for value in item["fieldValues"]["nodes"]}
    assert values["Lane"] == "api" and values["Size"] == "s"


def test_scheduler_sets_status_and_board_failure_does_not_fail_tick(git_history, tmp_path, monkeypatch, caplog, capsys):
    from dataclasses import replace

    from foragent import board
    from foragent.scheduler import tick
    from test_orch_review_handoff import tick_context

    case, main, argv, config = initialized_case(git_history, tmp_path, monkeypatch)
    case.ctx.config = replace(case.config, board=config["board"], approvers=["human"])
    case.store.finish_run(1, outcome="blocked")
    case.tracker.remove_labels(1, ["running", "approved"])
    case.tracker.record_label(1, "approved", "human")
    calls = []

    def set_status(issue, status):
        calls.append((getattr(issue, "number", issue), status))
        raise OSError("offline board unavailable")

    monkeypatch.setattr(board, "set_status", set_status)
    report = tick(tick_context(case, start_runs=False))
    assert report.skipped is None
    assert "ready" in case.tracker.get_issue(1).labels
    assert (1, "Ready") in calls
    assert caplog.records or capsys.readouterr().err
