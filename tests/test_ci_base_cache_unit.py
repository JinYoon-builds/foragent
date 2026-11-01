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
