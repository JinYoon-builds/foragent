"""Publish data artifacts in a trusted workflow_run job with no PR execution."""

import json
import os
from pathlib import Path
from urllib.request import Request, urlopen

from foragent.ci_transport import decode_report, encode_report
from foragent.ci_workflow import WORKFLOW


def publish(payload, repository, head_sha, run_url):
    if payload["repository"] != repository or payload["head_sha"] != head_sha:
        raise ValueError("artifact does not belong to the triggering PR")
    # Success means measurement completed; the runner compares passing sets.
    check = {"name": f"foragent-acceptance/{payload['shard_index']}", "head_sha": head_sha,
             "details_url": run_url, "status": "completed", "conclusion": "success", "output": {
                 "title": "Acceptance comparison", "summary": "Base/head passing sets", "text": encode_report(payload)}}
    request = Request(f"https://api.github.com/repos/{repository}/check-runs", method="POST",
                      data=json.dumps(check).encode(), headers={"Authorization": f"Bearer {os.environ['GH_TOKEN']}",
                      "Accept": "application/vnd.github+json", "Content-Type": "application/json",
                      "X-GitHub-Api-Version": "2022-11-28"})
    with urlopen(request) as response:
        response.read()


def main():
    repository = os.environ["FA_REPOSITORY"]
    run = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())["workflow_run"]
    if (run["path"].split("@", 1)[0] != WORKFLOW or run["event"] != "pull_request"
            or run["repository"]["full_name"] != repository
            or run["head_repository"]["full_name"] != repository or run["conclusion"] != "success"):
        raise ValueError("untrusted measurement workflow")
    count = int(os.environ["FA_SHARDS"])
    directory = Path(os.environ["FA_REPORT_DIR"])
    paths = sorted(directory.glob("shard-*.json"))
    if not paths:
        print("No acceptance measurements to publish.")
        return
    if {path.name for path in paths} != {f"shard-{index}.json" for index in range(count)}:
        raise ValueError("missing shard artifacts")
    payloads = []
    for index in range(count):
        payload = decode_report((directory / f"shard-{index}.json").read_text())
        if (payload["shard_index"] != index or payload["shard_count"] != count
                or payload["head_sha"] != run["head_sha"] or payload["repository"] != repository):
            raise ValueError("artifact identity does not match triggering run")
        payloads.append(payload)
    run_url = f"https://github.com/{repository}/actions/runs/{os.environ['GITHUB_RUN_ID']}"
    for payload in payloads:
        publish(payload, repository, run["head_sha"], run_url)


if __name__ == "__main__":
    main()
