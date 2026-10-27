"""Boundary tests for irreversible-path detection, registration and the always_human writer (#785)."""

from datetime import UTC, datetime
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from foragent import config as config_module
from foragent import irreversible_paths
from foragent.config import add_always_human, load_config
from foragent.policy import merge_decision, touched
from foragent.state import StateStore

NOW = datetime(2026, 10, 27, 12, tzinfo=UTC)
DEPLOY = ".github/workflows/deploy.yml"


def _write(root: Path, files: dict[str, str]) -> None:
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)


@pytest.mark.parametrize("before,after", [
    ("review: fast\n", "review: fast\nalways_human: [db/**]\n"),
    ("review: fast\nalways_human: [a/**]  # keep\n", "review: fast\nalways_human: [a/**, db/**]  # keep\n"),
    ("always_human:\n  - a/**  # keep\nreview: fast\n", "always_human:\n  - a/**  # keep\n  - db/**\nreview: fast\n"),
    ("{review: fast}\n", "{review: fast, always_human: [db/**]}\n"),
    ("", "always_human: [db/**]\n"),
])
def test_add_always_human_edits_only_its_key(tmp_path, before, after):
    (tmp_path / ".foragent.yml").write_text(before)
    assert add_always_human(tmp_path, "db/**")[-1] == "db/**"
    assert (tmp_path / ".foragent.yml").read_text() == after
    add_always_human(tmp_path, "db/**")
    assert (tmp_path / ".foragent.yml").read_text() == after, "An existing glob is not added twice"


@pytest.mark.parametrize("trigger,found", [
    ("{push: {tags: ['v*']}}", False),
    ("{push: {branches: ['ma*']}}", True),
    ("{push: {branches-ignore: [main]}}", False),
    ("{push: {branches-ignore: [dev]}}", True),
    ("pull_request", False),
])
def test_push_trigger_filters(tmp_path, trigger, found):
    _write(tmp_path, {DEPLOY: f"on: {trigger}\njobs:\n  app:\n    steps:\n      - run: ./deploy\n"})
    assert (DEPLOY in irreversible_paths.detect(tmp_path, base_branch="main")) is found


@pytest.mark.parametrize("job,found", [
    ({"steps": [{"run": "twine upload dist/*"}]}, True),
    ({"steps": [{"run": "./deployment-check"}]}, False),
    ({"steps": [{"uses": "example/deployment-check@v1"}]}, False),
    ({"steps": [{"uses": "example/release-action@v1"}]}, True),
    ({"name": "Release notes", "steps": [{"run": "true"}]}, True),
    ({"environment": "${{ inputs.target }}", "steps": [{"run": "true"}]}, True),
    ({"environment": "staging", "steps": [{"run": "true"}]}, False),
])
def test_deploy_words(tmp_path, job, found):
    _write(tmp_path, {DEPLOY: yaml.safe_dump({"on": "push", "jobs": {"app": job}})})
    assert (DEPLOY in irreversible_paths.detect(tmp_path, base_branch="main")) is found


@pytest.mark.parametrize("base,patterns,key,found", [
    ("release/1", ["release/[0-9]+"], "branches", True),
    ("release/1a", ["release/[0-9]+"], "branches", False),
    ("release/1", ["release/*"], "branches", True),
    ("release/a/b", ["release/*"], "branches", False),
    ("release/a/b", ["release/**"], "branches", True),
    ("main", ["mai?n"], "branches", True),
    ("main", ["**", "!main"], "branches", False),
    ("main", ["!main", "main"], "branches", True),
    ("main", ["m\\*n"], "branches", False),
    ("release/1", ["release/[0-9]+"], "branches-ignore", False),
    ("release/a/b", ["release/*"], "branches-ignore", True),
])
def test_github_branch_filter_patterns(tmp_path, base, patterns, key, found):
    _write(tmp_path, {DEPLOY: yaml.safe_dump({"on": {"push": {key: patterns}},
                                              "jobs": {"app": {"steps": [{"run": "./deploy"}]}}})})
    assert (DEPLOY in irreversible_paths.detect(tmp_path, base_branch=base)) is found


def test_glob_escapes_pattern_characters_in_directory_names(tmp_path):
    _write(tmp_path, {"app[1]/migrations/1.sql": "x"})
    glob = irreversible_paths.detect(tmp_path, base_branch="main")[0]
    assert touched(["app[1]/migrations/2.sql"], [glob]) and not touched(["app1/migrations/2.sql"], [glob])


def test_migrations_at_depth_but_not_under_tests(tmp_path):
    _write(tmp_path, {"services/api/db/migrate/1.rb": "x", "tests/migrations/1.sql": "x",
                      ".infra/migrations/001.sql": "x", "docs/migrations/1.sql": "x",
                      "services/api/tests/migrations/1.sql": "x", "services/api/docs/migrations/1.sql": "x",
                      "services/tests/fixtures/prisma/migrations/1.sql": "x"})
    assert irreversible_paths.detect(tmp_path, base_branch="main") == [".infra/migrations/**",
                                                                        "services/api/db/migrate/**"]


class _TreeGit:
    """Git trees by ref, as the real GitRepo reads them."""

    def __init__(self, trees):
        self.trees = trees

    def tree_files(self, ref):
        return list(self.trees[ref])

    def file_at(self, ref, path):
        return self.trees[ref].get(path)


def test_pr_removing_a_base_only_path_is_still_held(tmp_path):
    ctx, _ = _ctx(tmp_path, {})
    workflow = "on: push\njobs:\n  app:\n    steps:\n      - run: ./deploy\n"
    git = _TreeGit({"HEAD": {"src/app.py": "x"},
                    "origin/main": {DEPLOY: workflow, "migrations/001.sql": "x", "src/app.py": "x"}})
    irreversible_paths.register(ctx, 4, git, [DEPLOY, "migrations/001.sql"])
    assert sorted(ctx.config.always_human) == [DEPLOY, "migrations/**"]
    assert sorted(event["path"] for event in _events(ctx.store)) == [DEPLOY, "migrations/**"]


def test_concurrent_first_touches_announce_once_and_both_hold(tmp_path):
    ctx, head = _ctx(tmp_path, {"migrations/001.sql": "x"})
    other = SimpleNamespace(**vars(ctx))  # Loaded its config before the first run wrote the file.
    irreversible_paths.register(ctx, 4, head, ["migrations/001.sql"])
    irreversible_paths.register(other, 5, head, ["migrations/002.sql"])
    assert [event["path"] for event in _events(ctx.store)] == ["migrations/**"]
    assert other.config.always_human == ["migrations/**"], "The second run follows the written file"
    assert load_config(ctx.repo_root).always_human == ["migrations/**"]


def test_writer_keeps_global_always_human(tmp_path):
    global_path = Path.home() / ".config/foragent/config.yml"
    global_path.parent.mkdir(parents=True, exist_ok=True)
    global_path.write_text("always_human: [private/**]\n")
    (tmp_path / ".foragent.yml").write_text("review: fast\n")
    add_always_human(tmp_path, "migrations/**")
    assert load_config(tmp_path).always_human == ["private/**", "migrations/**"]


def _ctx(tmp_path, head_files):
    repo, head = tmp_path / "repo", tmp_path / "head"
    repo.mkdir()
    (repo / ".foragent.yml").write_text("review: fast\n")
    _write(head, head_files)
    store = StateStore(tmp_path / "state", now=lambda: NOW)
    ctx = SimpleNamespace(repo_root=repo, store=store, config=load_config(repo), now=lambda: NOW)
    return ctx, SimpleNamespace(worktree_root=lambda: head)


def _events(store):
    path = store.root / "orch-events.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def test_head_only_path_registers_once_and_failed_write_still_holds(tmp_path, monkeypatch):
    ctx, head = _ctx(tmp_path, {"migrations/001.sql": "x"})

    def broken(*args):
        raise config_module.ConfigError("read-only")

    monkeypatch.setattr(config_module, "add_always_human", broken)
    irreversible_paths.register(ctx, 4, head, ["migrations/001.sql"])
    assert merge_decision(changed_paths=["migrations/001.sql"], issue_labels=[], config=ctx.config).kind == "hold"
    assert [event["path"] for event in _events(ctx.store)] == ["migrations/**"]
    assert "read-only" in _events(ctx.store)[0]["summary"]

    # A person removed it (here: it never reached the file); the announcement is not repeated.
    monkeypatch.undo()
    ctx.config = load_config(ctx.repo_root)
    irreversible_paths.register(ctx, 5, head, ["migrations/002.sql"])
    assert len(_events(ctx.store)) == 1 and ctx.config.always_human == []
    assert "always_human" not in (ctx.repo_root / ".foragent.yml").read_text()


def test_untouched_detected_path_changes_nothing(tmp_path):
    ctx, head = _ctx(tmp_path, {"migrations/001.sql": "x"})
    irreversible_paths.register(ctx, 4, head, ["src/app.py"])
    assert _events(ctx.store) == [] and ctx.config.always_human == []
    assert (ctx.repo_root / ".foragent.yml").read_text() == "review: fast\n"


def test_detect_at_reads_a_real_git_ref(tmp_path):
    import subprocess

    from foragent.git import GitRepo

    _write(tmp_path, {DEPLOY: "on: push\njobs:\n  app:\n    steps:\n      - run: npm publish\n",
                      "db/migrate/1.rb": "x"})
    for args in (["init", "-q"], ["add", "-A"]):
        subprocess.run(["git", *args], cwd=tmp_path, check=True)
    tree = subprocess.run(["git", "write-tree"], cwd=tmp_path, check=True, capture_output=True, text=True).stdout.strip()
    (tmp_path / "db/migrate/1.rb").unlink()  # Working-tree edits do not change the stored tree.
    assert irreversible_paths.detect_at(GitRepo(tmp_path), tree, base_branch="main") == [DEPLOY, "db/migrate/**"]
