"""Entry point for benchmark commands."""

import argparse
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import subprocess
import sys

import yaml

from foragent.state import StateStore


def _load_scenario(directory: Path) -> dict:
    path = directory / "scenario.yml"
    scenario = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(scenario, dict):
        raise ValueError(f"Invalid scenario: {path} must contain a mapping")
    for field in ("name", "brief"):
        if not isinstance(scenario.get(field), str) or not scenario[field].strip():
            raise ValueError(f"Invalid scenario: {path} needs a non-empty {field}")
    checks = scenario.get("checks")
    if not isinstance(checks, list) or any(not isinstance(check, str) or not check.strip() for check in checks):
        raise ValueError(f"Invalid scenario: {path} checks must be a list of shell commands")
    return scenario


def _timestamp(value: str) -> datetime:
    timestamp = datetime.fromisoformat(value)
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError(f"Timestamp must include a timezone: {value}")
    return timestamp


def _record(args: argparse.Namespace) -> int:
    directory = args.scenario_dir.resolve()
    scenario = _load_scenario(directory)
    started = _timestamp(args.started)
    finished = _timestamp(args.finished) if args.finished is not None else datetime.now(UTC)
    if finished < started:
        raise ValueError("--finished must be at or after --started")

    runs = merged = retries = None
    if args.state_dir is not None:
        history = [run for run in StateStore(args.state_dir).read_history()
                   if started <= _timestamp(run.started_at) <= finished]
        runs = len(history)
        merged = sum(run.outcome == "merged" for run in history)
        retries = sum(run.attempts - 1 for run in history)

    env = {**os.environ, "FA_BENCH_SCENARIO": str(directory)}
    passed = 0
    for check in scenario["checks"]:
        result = subprocess.run(["sh", "-c", check], cwd=args.repo, env=env, check=False)
        passed += result.returncode == 0

    row = {
        "scenario": scenario["name"], "arm": args.arm,
        "started": started.isoformat(), "finished": finished.isoformat(),
        "elapsed_seconds": int((finished - started).total_seconds()),
        "checks_passed": passed, "checks_total": len(scenario["checks"]),
        "runs": runs, "merged": merged, "retries": retries,
        "tokens": args.tokens, "interventions": args.interventions,
    }
    path = directory / "results.jsonl"
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(f"Recorded {scenario['name']} arm {args.arm}: {passed}/{len(scenario['checks'])} checks passed → {path}")
    return 0


def run(args: argparse.Namespace) -> int:
    if args.action == "record":
        try:
            return _record(args)
        except (OSError, ValueError, TypeError, KeyError, yaml.YAMLError) as exc:
            print(f"bench record: {exc}", file=sys.stderr)
            return 1
    print(f"not implemented yet: {args.action}", file=sys.stderr)
    return 2
