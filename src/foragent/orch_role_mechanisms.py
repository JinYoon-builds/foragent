"""New fa subcommands and shelf places for the docs owner's role signal (Jin r1317).

When a merge adds a subcommand to the fa command tree or a place to the shelf, the docs
role's signal fires naming it, so the mechanisms map (docs/mechanisms.md) is checked the
same day.

Source: the inventory is read from what was merged. When the repository carries FA's own
source (``src/foragent``), it is read from that source at ``origin/<base>`` in a separate
Python process, since the gate itself runs an installed copy that a merge does not change.
Elsewhere it is read from the running package.

Writer: ``record`` (called only by the gate's raise step, ``orch_role_raise.raise_due``) is
the one writer of ``<state>/mechanisms.json``. It holds ``known``, every mechanism the gate
has seen, ``added``, the ones that appeared since, and ``source``, the commit the inventory
was last read from. The first observation only records ``known``. Read from the running
package, a mechanism counts as added only with a recent merge of FA's own source, so an FA
upgrade in another project joins ``known`` silently.

Reader: ``numbers`` derives the signal's facts: the added mechanisms no answered docs signal
was raised with. Only the answer to a request that named a mechanism retires it, so a
mechanism added while another request is open is named by the next request.
"""

from __future__ import annotations

import argparse
import fnmatch
import io
import json
import subprocess
import sys
import tarfile
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

from foragent import orch_inbox
from foragent.state import StateStore

KIND = "new_mechanisms"
DOCS_ROLE = "docs"
DOCS_SIGNAL = {"paths": [], "kinds": [KIND]}
# Where FA's own mechanisms are defined; only a merge that changed these files adds one.
PACKAGE = "src/foragent"
SOURCES = (f"{PACKAGE}/**",)
MAP = Path("docs/mechanisms.md")
COMMAND = "command"
PLACE = "place"
READ_TIMEOUT = 120

# Run in a separate process against an extracted source tree (argv[1] is its src folder).
_INVENTORY = """
import argparse, json, sys
sys.path.insert(0, sys.argv[1])
from foragent import cli, shelf
found = []
def walk(parser, prefix):
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for name, child in dict.items(action.choices):
                found.append("command:" + " ".join(["fa", *prefix, name]))
                walk(child, [*prefix, name])
walk(cli._parser(), [])
found += ["place:" + place.name for place in shelf.places()]
print(json.dumps(found))
"""


def _path(store: StateStore) -> Path:
    return store.root / "mechanisms.json"


def commands() -> list[str]:
    """Every subcommand path of the running fa command tree (``fa orch inbox``)."""
    from foragent import cli

    found: list[str] = []

    def walk(parser: argparse.ArgumentParser, prefix: list[str]) -> None:
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                for name, child in dict.items(action.choices):
                    found.append(" ".join(["fa", *prefix, name]))
                    walk(child, [*prefix, name])

    walk(cli._parser(), [])
    return found


def running() -> set[str]:
    """Each mechanism of the running package, keyed ``command:<path>`` or ``place:<name>``."""
    from foragent import shelf

    return {f"{COMMAND}:{path}" for path in commands()} | {f"{PLACE}:{place.name}" for place in shelf.places()}


def _git(root: Path, *args: str, binary: bool = False) -> Any:
    result = subprocess.run(["git", *args], cwd=root, capture_output=True, text=not binary,
                            timeout=READ_TIMEOUT, check=False)
    return result.stdout if result.returncode == 0 else None


def merged_commit(root: Path, base: str) -> str | None:
    """The merged commit whose FA source defines the inventory, or None when the repository
    carries no FA source (then the running package is the source)."""
    if not (Path(root) / PACKAGE / "cli.py").is_file():
        return None
    for ref in (f"origin/{base}", base):
        sha = _git(root, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
        if sha and sha.strip():
            return sha.strip()
    return None


def merged(root: Path, sha: str) -> set[str] | None:
    """The inventory of FA's source at ``sha``; None when it cannot be read."""
    archive = _git(root, "archive", "--format=tar", sha, PACKAGE, binary=True)
    if not archive:
        return None
    with tempfile.TemporaryDirectory(prefix="fa-mechanisms-") as folder:
        with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
            tar.extractall(folder, filter="data")
        result = subprocess.run([sys.executable, "-I", "-c", _INVENTORY, str(Path(folder) / "src")],
                                cwd=folder, capture_output=True, text=True, timeout=READ_TIMEOUT, check=False)
    if result.returncode != 0:
        return None
    try:
        return set(json.loads(result.stdout.strip().splitlines()[-1]))
    except (ValueError, IndexError):
        return None


def _load(store: StateStore) -> dict[str, Any] | None:
    try:
        data = json.loads(_path(store).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and isinstance(data.get("known"), list) else None


def _merged_source(files: set[str]) -> bool:
    return any(fnmatch.fnmatchcase(path, glob) for path in files for glob in SOURCES)


def _raised(record: dict[str, Any]) -> set[str]:
    """The mechanisms one docs request was raised with."""
    numbers = (((record.get("facts") or {}).get("kinds") or {}).get(KIND) or {}).get("numbers") or {}
    return ({f"{COMMAND}:{path}" for path in numbers.get("commands") or []}
            | {f"{PLACE}:{name}" for name in numbers.get("places") or []})


def _covered(key: str, covered: set[str]) -> bool:
    """Whether an answer covered ``key``: named itself, or a command under a named new group."""
    return key in covered or (key.startswith(f"{COMMAND}:")
                              and any(other.startswith(f"{COMMAND}:") and key.startswith(other + " ")
                                      for other in covered))


def answered(store: StateStore) -> set[str]:
    """Mechanisms named by a docs signal request that has been answered."""
    covered: set[str] = set()
    for record in orch_inbox.requests(store):
        if (record.get("from") == "gate" and record.get("role") == DOCS_ROLE
                and record.get("signal") == DOCS_SIGNAL and record.get("status") == "done"):
            covered |= _raised(record)
    return covered


def record(store: StateStore, now: datetime, files: set[str], root: Path, base: str) -> None:
    """Record mechanisms that appeared since the last observation; ``files`` are recent merged files."""
    data = _load(store)
    sha = merged_commit(root, base)
    if sha is not None and data is not None and data.get("source") == sha:
        return  # The merged source has not moved since it was read.
    seen = merged(root, sha) if sha is not None else running()
    if seen is None:
        return  # Unreadable this tick; never fall back to another source and see false changes.
    if data is None:
        data = {"known": sorted(seen), "added": {}}
    else:
        added = dict(data.get("added") or {})
        unknown = seen - set(data["known"])
        # Read from the merged source, every new item came with a change to the base branch; from
        # the running package, only a recent merge of FA's own source counts (an upgrade does not).
        if unknown and (sha is not None or _merged_source(files)):
            added.update({key: now.isoformat() for key in unknown})
        data = {"known": sorted(set(data["known"]) | seen),
                "added": {key: at for key, at in added.items() if key in seen}}
    if sha is not None:
        data["source"] = sha
    path = _path(store)
    text = json.dumps(data, indent=2, sort_keys=True) + "\n"
    if path.is_file() and path.read_text(encoding="utf-8") == text:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(text, encoding="utf-8")
    temp.replace(path)


def numbers(store: StateStore) -> dict[str, list[str]] | None:
    """The added mechanisms no answered docs signal named yet, or None when there are none.

    A new command is listed by its top-most new path only: the subcommands of a new group
    come with it.
    """
    data = _load(store)
    if data is None or not data.get("added"):
        return None
    covered = answered(store)
    keys = [key for key in data["added"] if not _covered(key, covered)]
    paths = sorted(key.split(":", 1)[1] for key in keys if key.startswith(f"{COMMAND}:"))
    tops = [path for path in paths if not any(path.startswith(other + " ") for other in paths)]
    places = sorted(key.split(":", 1)[1] for key in keys if key.startswith(f"{PLACE}:"))
    if not tops and not places:
        return None
    return {"commands": tops, "places": places}


def describe(found: dict[str, Any] | None) -> str:
    """The new mechanisms in words, for the docs owner's request."""
    found = found or {}
    parts = [f"`{path}`" for path in found.get("commands") or []]
    parts += [f"shelf place `{name}`" for name in found.get("places") or []]
    return ", ".join(parts)
