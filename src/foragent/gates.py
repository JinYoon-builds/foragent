"""Every Foragent gate installed on this machine, found from launchd plists and state folders.

A gate is a scheduled tick for one repository. It is found two ways: a
dev.foragent.*.plist in ~/Library/LaunchAgents, and a state folder under the
machine data root holding gate-heartbeat.json or runs.jsonl. A plist and a state
folder are one gate only when they resolve to the same state root.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import plistlib
import re
import shlex
from typing import Any

import yaml

from foragent import capacity, launchd
from foragent.state import StateStore

DEFAULT_INTERVAL = 300
# A loaded gate is stale when its last tick is older than this many intervals.
STALE_TICKS = 3
_GITHUB_URL = re.compile(r"github\.com[/:]([^/\s]+/[^/\s#?]+?)(?:\.git)?(?:[/#?]|$)")


@dataclass
class _Gate:
    repo: str
    state_root: Path | None = None
    repo_root: Path | None = None
    plists: list[Path] = field(default_factory=list)
    jobs: list[launchd.InstalledJob] = field(default_factory=list)
    interval: int = DEFAULT_INTERVAL


def _plist_gates(home: Path | None) -> list[_Gate]:
    directory = (Path(home) if home is not None else Path.home()) / "Library/LaunchAgents"
    domain = f"gui/{os.getuid()}"
    gates = []
    for path in sorted(directory.glob("dev.foragent.*.plist")):
        try:
            data = plistlib.loads(path.read_bytes())
            label = data["Label"]
            arguments = [str(value) for value in data.get("ProgramArguments") or []]
            root = Path(arguments[arguments.index("--repo-root") + 1]).expanduser().resolve()
        except (OSError, ValueError, IndexError, KeyError, TypeError, AttributeError,
                plistlib.InvalidFileException):
            # Unreadable: its repository is unknown, so it is listed by its path.
            gates.append(_Gate(repo=str(path), plists=[path]))
            continue
        if not isinstance(label, str) or not label.startswith("dev.foragent."):
            continue
        interval = data.get("StartInterval")
        gate = _Gate(repo=str(root), repo_root=root, plists=[path],
                     jobs=[launchd.InstalledJob(path, label, root, domain)],
                     interval=interval if isinstance(interval, int) and interval > 0 else DEFAULT_INTERVAL)
        if (repo := _configured_repo(root)) is not None:
            gate.repo = repo
        elif (repo := _remote_repo(root)) is None:
            gates.append(gate)  # Neither a repo key nor a GitHub remote: no state to read.
            continue
        # The tick's own rule: the configured repo, else the origin remote.
        gate.state_root = StateStore.for_repo(repo).root.resolve()
        gates.append(gate)
    return gates


def _configured_repo(root: Path) -> str | None:
    """The repo key of the gate's .foragent.yml; None when missing or unreadable."""
    try:
        data = yaml.safe_load((root / ".foragent.yml").read_text(encoding="utf-8"))
    except (OSError, ValueError, yaml.YAMLError):
        return None
    repo = data.get("repo") if isinstance(data, dict) else None
    return repo if isinstance(repo, str) and repo.strip() else None


def _remote_repo(root: Path) -> str | None:
    """The origin remote's owner/name, as a tick finds it without a repo key."""
    from foragent.git import GitError, GitRepo

    if not root.is_dir():
        return None
    try:
        return GitRepo(root).remote_slug()
    except (GitError, OSError):
        return None


def _state_folders(data_root: Path) -> list[Path]:
    try:
        children = sorted(data_root.iterdir())
    except OSError:
        return []
    return [child.resolve() for child in children if child.is_dir()
            and ((child / "gate-heartbeat.json").is_file() or (child / "runs.jsonl").is_file())]


def _named_repo(record: Any) -> str | None:
    """The owner/name a run or issue record names, by a repo key or a GitHub URL."""
    if not isinstance(record, dict):
        return None
    repo = record.get("repo")
    if isinstance(repo, str) and repo.count("/") == 1:
        return repo
    for key in ("url", "html_url", "pr_url"):
        if isinstance(value := record.get(key), str) and (match := _GITHUB_URL.search(value)):
            return match[1]
    return None


def _moment(value: Any) -> datetime | None:
    """A record timestamp as an aware datetime; naive values are UTC, junk is None."""
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def _record_time(record: dict[str, Any], keys: tuple[str, ...]) -> datetime | None:
    """The latest of a record's own timestamps, including its observed issue facts."""
    observed = record.get("observed") if isinstance(record.get("observed"), dict) else {}
    moments = [moment for key in keys for moment in (_moment(record.get(key)), _moment(observed.get(key)))
               if moment is not None]
    return max(moments, default=None)


RUN_TIMES = ("finished_at", "last_progress_at", "started_at")
ISSUE_TIMES = ("updated_at", "merged_at", "created_at")


PR_TIMES = ("merged_at", "updated_at")


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _records(root: Path) -> list[tuple[dict[str, Any], tuple[str, ...]]]:
    """Every run and issue record of a state folder, with the keys that time it.

    Runs: the current record of each run (runs/<n>.json) and every finished entry
    (runs.jsonl). Issues: the issue rows (issues.json) and their merged PRs
    (merged_prs.json). Approval records name no repository, so they cannot name one.
    """
    found: list[tuple[dict[str, Any], tuple[str, ...]]] = []
    try:
        current = sorted((root / "runs").glob("*.json"))
    except OSError:
        current = []
    found += [(record, RUN_TIMES) for path in current if isinstance(record := _read_json(path), dict)]
    try:
        lines = (root / "runs.jsonl").read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    for line in lines:
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            found.append((record, RUN_TIMES))
    for name, field, keys in (("issues.json", "issues", ISSUE_TIMES), ("merged_prs.json", "prs", PR_TIMES)):
        data = _read_json(root / name)
        rows = data.get(field) if isinstance(data, dict) else None
        rows = list(rows.values()) if isinstance(rows, dict) else rows if isinstance(rows, list) else []
        found += [(row, keys) for row in rows if isinstance(row, dict)]
    return found


def _recorded_repo(root: Path) -> str:
    """The repo the newest run or issue record names, else the folder name with '__' as '/'.

    Run and issue records compete on their own timestamps; an untimed record
    ranks below every timed one.
    """
    floor = datetime.min.replace(tzinfo=UTC)
    named = [(_record_time(record, keys) or floor, repo) for record, keys in _records(root)
             if (repo := _named_repo(record)) is not None]
    if not named:
        return root.name.replace("__", "/")
    # max keeps the first of equal times; reversed makes the later-written record win ties.
    return max(reversed(named), key=lambda item: item[0])[1]


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # Another user's process still exists.
    except OSError:
        return False
    return True


def _running(root: Path | None) -> int:
    """Active run records whose runner is alive; a record without a pid cannot be checked."""
    if root is None or not root.is_dir():
        return 0
    try:
        runs = StateStore(root).active_runs()
    except (OSError, ValueError, TypeError, KeyError):
        return 0
    return sum(1 for run in runs if run.pid is None or _alive(run.pid))


def _scheduler(gate: _Gate) -> str:
    """loaded, not loaded or unknown; a gate found only from state has no job to ask about."""
    if gate.plists and not gate.jobs:
        return "unknown"  # An unreadable plist names no job launchd could be asked about.
    states = {job.state() for job in gate.jobs}
    if "loaded" in states:
        return "loaded"
    return "unknown" if "unknown" in states else "not loaded"


def collect(now: datetime, *, current: Path | None = None, home: Path | None = None,
            data_root: Path | None = None) -> list[dict[str, Any]]:
    """One row per installed gate: repo, last tick, running runs and scheduler state."""
    gates: dict[Any, _Gate] = {}
    for gate in _plist_gates(home):
        key = gate.state_root or gate.plists[0]
        if (known := gates.get(key)) is not None:
            known.plists += gate.plists
            known.jobs += gate.jobs
            known.interval = min(known.interval, gate.interval)
        else:
            gates[key] = gate
    for root in _state_folders(data_root or capacity.machine_root()):
        if root not in gates:
            gates[root] = _Gate(repo=_recorded_repo(root), state_root=root)
    current = Path(current).resolve() if current is not None else None
    rows = []
    for gate in gates.values():
        tick = (StateStore(gate.state_root).heartbeats()["gate"]
                if gate.state_root is not None else None)
        scheduler = _scheduler(gate)
        loaded = scheduler == "loaded"
        stale = loaded and (tick is None or (now - tick).total_seconds() > STALE_TICKS * gate.interval)
        rows.append({
            "repo": gate.repo,
            "repo_root": str(gate.repo_root) if gate.repo_root is not None else None,
            "state_root": str(gate.state_root) if gate.state_root is not None else None,
            "plists": [str(path) for path in gate.plists],
            "last_tick": tick.isoformat() if tick is not None else None,
            "running_runs": _running(gate.state_root),
            "loaded": loaded,
            "scheduler": scheduler,
            "interval_seconds": gate.interval,
            "stale": stale,
            "current": current is not None and gate.state_root == current,
            # The latest launchd reload of this gate; None until one is attempted.
            "reload": launchd.reload_fact(gate.state_root),
        })
    return sorted(rows, key=lambda row: (not row["current"], row["repo"]))


def age(timestamp: str | None, now: datetime, language: str) -> str:
    """A short relative age such as '20 min ago'."""
    from foragent.i18n import text

    if timestamp is None:
        return text("gates_never", language)
    seconds = max(0, int((now - datetime.fromisoformat(timestamp)).total_seconds()))
    for unit, size in (("days", 86400), ("hours", 3600), ("minutes", 60)):
        if seconds >= size:
            return text(f"gates_age_{unit}", language, count=seconds // size)
    return text("gates_age_seconds", language, count=seconds)


def human_lines(rows: list[dict[str, Any]], now: datetime, language: str) -> list[str]:
    """The machine gate list for fa status and fa gate status."""
    from foragent.i18n import text

    if not rows:
        return [text("gates_none", language)]
    lines = [text("gates_heading", language)]
    for row in rows:
        tick = (text("gates_never", language) if row["last_tick"] is None
                else f"{row['last_tick']} ({age(row['last_tick'], now, language)})")
        scheduler = text(f"gates_scheduler_{row['scheduler'].replace(' ', '_')}", language)
        warning = f" ⚠ {text('gates_stale', language)}" if row["stale"] else ""
        lines.append(text("gates_row", language, repo=row["repo"], tick=tick,
                          running=row["running_runs"], scheduler=scheduler) + warning)
        lines += _reload_lines(row, language)
    return lines


def _reload_lines(row: dict[str, Any], language: str) -> list[str]:
    """The last failed reload, whether it recovered, and the command that reinstalls the gate."""
    from foragent.i18n import text

    fact = row.get("reload")
    # A pending (unloaded) reload is a failure even when no error text was recorded.
    if not isinstance(fact, dict) or not (fact.get("error") or fact.get("pending") or fact.get("loaded") is False):
        return []
    loaded = fact.get("loaded") is True and not fact.get("pending")
    key = "gates_reload_recovered" if loaded else "gates_reload_failed"
    error = " ".join(str(fact.get("error") or text("gates_reload_not_loaded", language)).split())
    lines = [text(key, language, at=fact.get("at"), attempts=fact.get("attempts"), error=error)]
    if row.get("repo_root"):
        command = shlex.join(["fa", "gate", "install", "--repo-root", row["repo_root"]])
        lines.append(text("gates_reload_command", language, command=command))
    return lines
