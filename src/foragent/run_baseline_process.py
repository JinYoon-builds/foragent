"""Entry point of the detached baseline builder started by `start_baseline_process`."""

from __future__ import annotations

from datetime import UTC, datetime
import json
import os
from pathlib import Path
from types import SimpleNamespace

from foragent import capacity
from foragent.config import Config
from foragent.git import GitRepo
from foragent.records import load_record
from foragent.state import StateStore


def main(argv: list[str]) -> None:
    from foragent.run_baseline import BaselineBuild, _build_baseline

    request = json.loads(argv[1])
    capacity.machine_root = lambda: Path(request["machine_root"])
    root = Path(request["repo_root"])
    # Every setting the build reads crosses the process boundary in the request.
    context = SimpleNamespace(
        repo_root=root, store=StateStore(Path(request["store_root"])), git=GitRepo(root),
        config=Config(acceptance_dir=request["acceptance_dir"], test_slots=request["test_slots"],
                      repo=request.get("repo")),
        now=lambda: datetime.now(UTC),
    )
    _build_baseline(context, load_record(BaselineBuild, request["build"]), os.fdopen(int(argv[2]), "a"))
