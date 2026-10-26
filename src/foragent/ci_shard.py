"""Measure exact base/head commits without any check publication credentials."""

import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
from time import monotonic

from foragent.ci_workflow import EXCLUSIONS, acceptance_command, measurement_scope, parse_exclusions, read_exclusions
from foragent.config import load_config
from foragent.git import GitRepo


def measure(root, command, acceptance, excluded, directory, side):
    if not (root / acceptance).is_dir():
        return {"complete": True, "collected_ids": [], "passing_ids": []}
    report = directory / f"{side}.json"
    environment = {key: value for key, value in os.environ.items()
                   if key not in {"VIRTUAL_ENV", "PYTHONPATH", "UV_PROJECT", "UV_PROJECT_ENVIRONMENT",
                                  "GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN",
                                  "ACTIONS_RUNTIME_TOKEN", "ACTIONS_ID_TOKEN_REQUEST_TOKEN", "GITHUB_OUTPUT",
                                  "GITHUB_ENV", "GITHUB_PATH", "FA_REPORT_FILE"}}
    environment.update(PYTHONPATH=str(directory), FA_REPORT=str(report), PYTHONDONTWRITEBYTECODE="1")
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


def main():
    started = monotonic()
    root = Path(os.environ.get("FA_CHECKOUT", Path.cwd()))
    if "FA_BASE_SHA" not in os.environ:
        print("Acceptance comparisons run locally (checks: local).")
        return
    head, base = os.environ["FA_HEAD_SHA"], os.environ["FA_BASE_SHA"]
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
            base_result = measure(base_root, scope["command"], config.acceptance_dir, excluded, directory, "base")
            head_result = measure(root, scope["command"], config.acceptance_dir, excluded, directory, "head")
            # A passing run proves only the requested revisions, never a tree
            # rewritten by the checks (including changes staged by a test).
            for checkout, revision, result in ((base_root, base, base_result), (root, head, head_result)):
                status = subprocess.run(["git", "status", "--porcelain=v1", "--untracked-files=no"],
                                        cwd=checkout, check=True, capture_output=True, text=True).stdout
                measured_head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=checkout,
                                               check=True, capture_output=True, text=True).stdout.strip()
                result["complete"] &= not status and measured_head == revision
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
