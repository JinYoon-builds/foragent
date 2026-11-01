"""Measure exact base/head commits without any check publication credentials."""

import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
from time import monotonic

from foragent.ci_base_cache import cache_scope, read_results
from foragent.ci_workflow import EXCLUSIONS, acceptance_command, measurement_scope, parse_exclusions, read_exclusions
from foragent.config import load_config
from foragent.git import GitRepo


def measure(root, command, acceptance, excluded, directory, side, *, cached_results=None, full_selection=False):
    if not (root / acceptance).is_dir():
        return {"complete": True, "collected_ids": [], "passing_ids": []}
    report = directory / f"{side}.json"
    environment = {key: value for key, value in os.environ.items()
                   if key not in {"VIRTUAL_ENV", "PYTHONPATH", "UV_PROJECT", "UV_PROJECT_ENVIRONMENT",
                                  "GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN",
                                  "ACTIONS_RUNTIME_TOKEN", "ACTIONS_ID_TOKEN_REQUEST_TOKEN", "GITHUB_OUTPUT",
                                  "GITHUB_ENV", "GITHUB_PATH", "FA_REPORT_FILE", "FA_BASE_CACHE_FILE",
                                  "FA_BASE_CACHE_MODE", "FA_CACHED_RESULTS"}}
    environment.update(PYTHONPATH=str(directory), FA_REPORT=str(report), PYTHONDONTWRITEBYTECODE="1")
    if cached_results:
        cached = directory / "cached-results.json"
        cached.write_text(json.dumps(cached_results), encoding="utf-8")
        environment["FA_CACHED_RESULTS"] = str(cached)
    if full_selection:
        environment.update(FA_SHARD="0", FA_SHARDS="1")
    selection = f"{command} {shlex.quote(acceptance)} -m 'not local_only' -p fa_measurement"
    selection += "".join(f" {shlex.quote('--ignore=' + path)}" for path in excluded)
    argv = ["/bin/sh", "-c", selection]
    if (root / "pyproject.toml").is_file():
        # Each checkout gets its own dependencies, including for bare `pytest`
        # commands; the head's editable installation must never test the base.
        argv = ["uv", "run", "--with", "pytest", *argv]
        environment.pop("UV_NO_SYNC", None)
    result = subprocess.run(argv, cwd=root, env=environment, check=False)
    if report.exists():
        value = json.loads(report.read_text())
        value["complete"] &= result.returncode == value.pop("exit_code", None)
        return value
    return {"complete": False, "collected_ids": [], "passing_ids": []}


def checkout_matches(root, revision):
    status = subprocess.run(["git", "status", "--porcelain=v1", "--untracked-files=no"],
                            cwd=root, check=True, capture_output=True, text=True).stdout
    actual = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root,
                            check=True, capture_output=True, text=True).stdout.strip()
    return not status and actual == revision


def produce(root, base, head):
    with tempfile.TemporaryDirectory(prefix="foragent-ci-") as temporary:
        directory = Path(temporary)
        config = load_config(root, global_path=directory / "no-global-config")
        ref = f"refs/heads/{config.base_branch}"
        event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8"))
        if not (os.environ.get("GITHUB_EVENT_NAME") == "push" and os.environ.get("GITHUB_REF") == ref
                and os.environ.get("GITHUB_SHA") == base == head
                and event.get("ref") == ref and event.get("after") == base
                and event.get("repository", {}).get("full_name") == os.environ["FA_REPOSITORY"]
                and checkout_matches(root, base)):
            raise ValueError("base records require a clean exact trusted base-branch push")
        if config.checks != "ci" or acceptance_command(config, root) is None:
            return
        scope = cache_scope(measurement_scope(config, root, []))
        shutil.copyfile(Path(__file__).with_name("ci_pytest.py"), directory / "fa_measurement.py")
        result = measure(root, scope["command"], config.acceptance_dir, [], directory, "base", full_selection=True)
        if not result["complete"] or not checkout_matches(root, base):
            raise ValueError("cannot publish an incomplete or modified base measurement")
        passing = set(result["passing_ids"])
        payload = {"schema_version": 1, "repository": os.environ["FA_REPOSITORY"], "base_sha": base,
                   "scope": scope, "producer": {"event": "push", "ref": ref, "run_id": os.environ["GITHUB_RUN_ID"]},
                   "results": {node: "passed" if node in passing else "nonpassing" for node in result["collected_ids"]}}
        Path(os.environ["FA_BASE_CACHE_FILE"]).write_text(json.dumps(payload), encoding="utf-8")


def main():
    started = monotonic()
    root = Path(os.environ.get("FA_CHECKOUT", Path.cwd()))
    if "FA_BASE_SHA" not in os.environ:
        print("Acceptance comparisons run locally (checks: local).")
        return
    head, base = os.environ["FA_HEAD_SHA"], os.environ["FA_BASE_SHA"]
    if os.environ.get("FA_BASE_CACHE_MODE") == "produce":
        produce(root, base, head)
        return
    actual = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root,
                            check=True, capture_output=True, text=True).stdout.strip()
    if actual != head:
        raise ValueError("checkout is not the PR head")
    git = GitRepo(root)
    excluded = read_exclusions(git, base)
    if excluded is None and (blob := git.blob_sha("HEAD", EXCLUSIONS)) is not None:
        # Legacy file protocol for runs that started before this change.
        excluded = parse_exclusions(git._call(["cat-file", "blob", blob]))
    if excluded is None:
        print("Acceptance comparisons run locally (PR head has no CI-mode exclusions trailer).")
        return
    with tempfile.TemporaryDirectory(prefix="foragent-ci-") as temporary:
        directory = Path(temporary)
        shutil.copyfile(Path(__file__).with_name("ci_pytest.py"), directory / "fa_measurement.py")
        base_root = directory / "base"
        subprocess.run(["git", "worktree", "add", "--detach", str(base_root), base], cwd=root, check=True)
        try:
            # Both checkouts use only the exact base's settings. Never load head
            # configuration, including to decide whether measurement is enabled.
            config = load_config(base_root, global_path=directory / "no-global-config")
            if config.checks != "ci" or acceptance_command(config, base_root) is None:
                print("Acceptance comparisons run locally (CI measurement disabled or unsupported command).")
                return
            scope = measurement_scope(config, base_root, excluded)
            if int(os.environ["FA_SHARDS"]) != config.ci_shards:
                raise ValueError("workflow shard layout does not match trusted base configuration")
            cached = read_results(os.environ.get("FA_BASE_CACHE_FILE"), os.environ["FA_REPOSITORY"], base, scope)
            base_result = measure(base_root, scope["command"], config.acceptance_dir, excluded, directory, "base",
                                  **({"cached_results": cached} if cached else {}))
            head_result = measure(root, scope["command"], config.acceptance_dir, excluded, directory, "head")
            # A passing run proves only the requested revisions, never a tree
            # rewritten by the checks (including changes staged by a test).
            for checkout, revision, result in ((base_root, base, base_result), (root, head, head_result)):
                result["complete"] &= checkout_matches(checkout, revision)
        finally:
            subprocess.run(["git", "worktree", "remove", "--force", str(base_root)], cwd=root, check=True)
    payload = {"schema_version": 1, "repository": os.environ["FA_REPOSITORY"], "base_sha": base, "head_sha": head,
               "shard_index": int(os.environ["FA_SHARD"]), "shard_count": int(os.environ["FA_SHARDS"]),
               "excluded": excluded, "scope": scope, "duration_seconds": monotonic() - started,
               "base": base_result, "head": head_result}
    output = json.dumps(payload, separators=(",", ":"))
    Path(os.environ["FA_REPORT_FILE"]).write_text(output, encoding="utf-8")
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as stream:
        stream.write("comparison=true\n")
    print(output)


if __name__ == "__main__":
    main()
