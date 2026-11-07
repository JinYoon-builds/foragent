"""Shared world for owner-job-slim-real (UX r1135).

Real settings: merge_owner on, merge_batch on, checks: ci (the merge-path world).
Real runner, public gate tick, merge owner, merge train, Git, state and
small-repository pytest; only GitHub, models and the clock are fake.

The factory's job files were first written by code that kept the serialized
acceptance reports in comparison.reports (#1957's ComparisonEvidence still
reads that form). `legacy` puts exactly that form on disk, the way 164 real
files hold it, with a unique marker in the report body so any copy of the
body is visible in any later file.
"""

import json
import os
from pathlib import Path

from foragent import ci_mode

MARK = "uxr1135-report-body-" + "y" * 200_000


def large_reports(case, monkeypatch):
    """GitHub's run metadata is large and identical on every read of the same head."""
    reports = case.tracker.pr_acceptance_reports

    def measured(head):
        items = reports(head)
        for item in items:
            if isinstance(item, dict) and isinstance(item.get("run"), dict):
                item["run"]["display_title"] = MARK
        return items

    monkeypatch.setattr(case.tracker, "pr_acceptance_reports", measured)


def job_dir(case):
    return Path(case.store.root) / "merge-owner"


def job_path(case, n):
    return job_dir(case) / f"{n}.json"


def legacy(case, n):
    """Rewrite N's job file in the pre-#1957 form: full serialized reports."""
    path = job_path(case, n)
    job = json.loads(path.read_text())
    assert job.get("comparison"), "Fixture: the queued job carries CI comparison evidence"
    reports = case.tracker.pr_acceptance_reports(job["comparison"]["head_sha"])
    body = ci_mode.serialize_reports(reports)
    assert MARK in body, "Fixture: the reports carry the body marker"
    job["comparison"]["reports"] = body
    path.write_text(json.dumps(job, indent=2) + "\n")
    assert MARK in path.read_text()
    return path


def bodies(case):
    """Job files on disk that still hold a full report body, with their sizes."""
    return {path.name: path.stat().st_size for path in sorted(job_dir(case).glob("*.json"))
            if MARK in path.read_text()}


def watch_job_writes(case, monkeypatch):
    """Record every job file the state writes (the store replaces files atomically).

    Observation only: the real replace still runs with the same arguments.
    """
    written = []
    replace = os.replace
    root = job_dir(case).resolve()

    def observed(src, dst, *args, **kwargs):
        target = Path(dst).resolve() if not isinstance(dst, int) else None
        if target is not None and target.parent == root and target.suffix == ".json":
            try:
                text = Path(src).read_text()
            except (OSError, UnicodeDecodeError, TypeError):
                text = ""
            try:
                phase = json.loads(text).get("phase")
            except ValueError:
                phase = None
            written.append({"name": target.name, "phase": phase, "body": MARK in text, "size": len(text)})
        return replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "replace", observed)
    return written
