"""Validation at the trusted base cache's record and transport boundaries."""

from contextlib import contextmanager
import json
from urllib.parse import parse_qs, urlparse

import pytest

from foragent import ci_base_cache, ci_shard


@pytest.mark.parametrize("damage", [None, "schema", "repository", "result", "results", "node"])
def test_record_validation_is_all_or_nothing(tmp_path, damage):
    scope = {"acceptance_dir": "tests/acceptance", "command": "pytest", "marker": "not local_only"}
    value = {"schema_version": 1, "repository": "owner/repo", "base_sha": "a" * 40,
             "scope": scope, "results": {"test.py::test_one": "passed", "test.py::test_two": "nonpassing"}}
    if damage == "schema":
        value["schema_version"] = True
    elif damage == "repository":
        value["repository"] = "other/repo"
    elif damage == "result":
        value["results"]["test.py::test_two"] = []
    elif damage == "results":
        value["results"] = []
    elif damage == "node":
        value["results"][""] = "passed"
    path = tmp_path / "cache.json"
    path.write_text(json.dumps(value))
    expected = value["results"] if damage is None else {}
    assert ci_base_cache.read_results(path, "owner/repo", "a" * 40,
                                      {**scope, "excluded": ["unrelated"], "shard_count": 7}) == expected


@pytest.mark.parametrize("cache_state", ["missing", "older-main"])
def test_unmeasured_publication_base_is_measured_at_its_exact_sha(git_history, tmp_path, monkeypatch, cache_state):
    import sys

    from conftest import ACCEPTANCE, git_command
    from foragent.ci_workflow import measurement_scope
    from foragent.config import Config

    root = git_history.clone
    execution = tmp_path / "executed.log"
    command = f"{sys.executable} -m pytest"
    git_history.update({
        ".foragent.yml": f"checks: ci\nci_shards: 1\ntest_command: {command}\n",
        "src/demo.py": "VALUE = 1\n",
        ACCEPTANCE: ("from pathlib import Path\n"
                     "def test_feature():\n"
                     f"    with Path({str(execution)!r}).open('a') as log: log.write(str(Path.cwd()) + '\\n')\n"),
    })
    measured = git_history.tip
    scope = ci_base_cache.cache_scope(measurement_scope(Config(test_command=command, ci_shards=1), root, []))
    cache = tmp_path / "base.json"
    original = json.dumps({"schema_version": 1, "repository": "owner/repo", "base_sha": measured,
                           "scope": scope, "results": {ACCEPTANCE + "::test_feature": "passed"}})
    if cache_state == "older-main":
        cache.write_text(original)

    published = "tests/acceptance/test_new_order.py"
    git_history.update({published: "from pathlib import Path\ndef test_order():\n"
                                  "    assert Path('src/demo.py').read_text() == 'VALUE = 1\\n'\n"})
    base = git_history.tip  # C skips push CI for this publication-only commit.
    git_history.update({"src/demo.py": "VALUE = 0\n"})
    head = git_history.tip
    git_command(root, "fetch", "-q", "origin")
    git_command(root, "merge", "-q", "--ff-only", "origin/main")
    report, output = tmp_path / "report.json", tmp_path / "output"
    for key, value in {"FA_CHECKOUT": str(root), "FA_REPOSITORY": "owner/repo",
                       "FA_BASE_SHA": base, "FA_HEAD_SHA": head, "FA_SHARD": "0", "FA_SHARDS": "1",
                       "FA_PR_CI": "true", "FA_BASE_CACHE_FILE": str(cache), "FA_REPORT_FILE": str(report),
                       "GITHUB_OUTPUT": str(output), "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
                       "PYTEST_ADDOPTS": ""}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("FA_BASE_CACHE_MODE", raising=False)
    monkeypatch.delenv("PYTEST_PLUGINS", raising=False)
    ci_shard.main()

    payload = json.loads(report.read_text())
    assert payload["base_sha"] == base != measured and payload["head_sha"] == head
    assert payload["base"]["complete"] and payload["head"]["complete"]
    assert set(payload["base"]["passing_ids"]) == {ACCEPTANCE + "::test_feature", published + "::test_order"}
    assert set(payload["head"]["passing_ids"]) == {ACCEPTANCE + "::test_feature"}
    assert output.read_text() == "comparison=true\nlost=1\n"
    executed = execution.read_text().splitlines()
    assert len(executed) == 2 and executed.count(str(root)) == 1  # Both exact checkouts ran the old test.
    assert (cache.read_text() if cache.exists() else None) == (original if cache_state == "older-main" else None)


@pytest.mark.parametrize("origin", ["base", "pr", "base-and-pr", "other", "api-error", "bad-json",
                                  "missing-field", "mismatched-key", "late-pr"])
def test_origin_guard_uses_github_inventory_and_fails_closed(tmp_path, monkeypatch, origin):
    path = tmp_path / "restored.json"
    original = b'{"producer":{"event":"push","ref":"refs/heads/main"}}'
    path.write_bytes(original)
    key = "foragent-acceptance-base-v1-" + "a" * 40
    for name, value in {"FA_BASE_CACHE_FILE": str(path), "FA_BASE_CACHE_KEY": key,
                        "FA_BASE_CACHE_MATCHED_KEY": key if origin != "mismatched-key" else "other",
                        "FA_BASE_CACHE_REF": "refs/heads/release/train", "GITHUB_REPOSITORY": "owner/repo",
                        "GITHUB_API_URL": "https://api.example.test", "GH_TOKEN": "secret"}.items():
        monkeypatch.setenv(name, value)
    requests = []

    @contextmanager
    def fetch(request, timeout):
        from io import StringIO
        requests.append(request)
        assert timeout == 30
        assert request.get_header("Authorization") == "Bearer secret"
        parsed = urlparse(request.full_url)
        assert parsed.path == "/repos/owner/repo/actions/caches"
        params = parse_qs(parsed.query)
        assert params["key"] == [key] and params["per_page"] == ["100"]
        if origin == "api-error":
            raise OSError("unavailable")
        if origin == "bad-json":
            yield StringIO("not json")
            return
        refs = {"base": ["refs/heads/release/train"], "pr": ["refs/pull/101/merge"],
                "base-and-pr": ["refs/heads/release/train", "refs/pull/202/merge"],
                "other": ["refs/heads/other"], "missing-field": [None]}.get(origin, [])
        if origin == "late-pr":
            refs = ["refs/heads/release/train"] * 100 if params["page"] == ["1"] else ["refs/pull/303/merge"]
        rows = [{"key": key, **({"ref": ref} if ref else {})} for ref in refs]
        yield StringIO(json.dumps({"actions_caches": rows}))

    monkeypatch.setattr(ci_base_cache, "urlopen", fetch)
    ci_base_cache.verify_restored_origin()
    if origin == "base":
        assert path.read_bytes() == original
    else:
        assert not path.exists()
    assert len(requests) == (0 if origin == "mismatched-key" else 2 if origin == "late-pr" else 1)


@pytest.mark.parametrize("damage", ["event", "ref", "sha", "event-ref", "event-after", "repository",
                                  "dirty-before", "dirty-after", "revision-after", "incomplete"])
def test_producer_cannot_publish_untrusted_or_incomplete_measurements(git_history, tmp_path, monkeypatch, damage):
    from conftest import git_command

    root = git_history.clone
    git_history.update({".foragent.yml": "checks: ci\ntest_command: pytest\n", "tracked.txt": "original"})
    base = git_history.tip
    git_command(root, "fetch", "-q", "origin")
    git_command(root, "merge", "-q", "--ff-only", "origin/main")
    cache = tmp_path / "base.json"
    event = {"ref": "refs/heads/main", "after": base, "repository": {"full_name": "owner/repo"}}
    if damage == "event-ref":
        event["ref"] = "refs/pull/1/merge"
    elif damage == "event-after":
        event["after"] = "b" * 40
    elif damage == "repository":
        event["repository"]["full_name"] = "other/repo"
    event_file = tmp_path / "push.json"
    event_file.write_text(json.dumps(event))
    for name, value in {"FA_CHECKOUT": str(root), "FA_REPOSITORY": "owner/repo", "FA_BASE_SHA": base,
                        "FA_HEAD_SHA": base, "FA_BASE_CACHE_MODE": "produce", "FA_BASE_CACHE_FILE": str(cache),
                        "GITHUB_EVENT_NAME": "pull_request" if damage == "event" else "push",
                        "GITHUB_REF": "refs/heads/other" if damage == "ref" else "refs/heads/main",
                        "GITHUB_SHA": "b" * 40 if damage == "sha" else base,
                        "GITHUB_EVENT_PATH": str(event_file), "GITHUB_RUN_ID": "123"}.items():
        monkeypatch.setenv(name, value)
    if damage == "dirty-before":
        (root / "tracked.txt").write_text("dirty")

    def measure(checkout, command, acceptance, excluded, directory, side, **kwargs):
        assert checkout == root and excluded == [] and kwargs == {"full_selection": True}
        if damage == "dirty-after":
            (root / "tracked.txt").write_text("dirty")
        if damage == "revision-after":
            revision = git_command(root, "-c", "user.name=Tests", "-c", "user.email=tests@example.com",
                                   "commit-tree", "HEAD^{tree}", "-p", "HEAD", "-m", "Unexpected revision")
            git_command(root, "update-ref", "HEAD", revision)
        return {"complete": damage != "incomplete", "collected_ids": [], "passing_ids": []}

    monkeypatch.setattr(ci_shard, "measure", measure)
    with pytest.raises(ValueError):
        ci_shard.main()
    assert not cache.exists()
