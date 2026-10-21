"""Wake the gate without delaying the operation that made work available."""

import logging
from pathlib import Path
import subprocess
import sys

from foragent.config import load_config
from foragent.git import GitRepo
from foragent.state import StateStore


def kick_tick(repo_root: Path) -> None:
    """Start a detached tick; its output and launch failures stay in kick.log."""
    try:
        root = Path(repo_root).resolve()
        repo = load_config(root).repo or GitRepo(root).remote_slug()
        directory = StateStore.for_repo(repo).root
        directory.mkdir(parents=True, exist_ok=True)
        with (directory / "kick.log").open("ab") as stream:
            try:
                subprocess.Popen(
                    [sys.executable, "-m", "foragent.cli", "gate", "tick", "--repo-root", str(root)],
                    cwd=root, stdin=subprocess.DEVNULL, start_new_session=True,
                    stdout=stream, stderr=stream,
                )
            except Exception as exc:
                stream.write(f"foragent: tick kick failed: {' '.join(str(exc).split())}\n".encode("utf-8"))
    except Exception:
        logging.getLogger(__name__).exception("Could not kick tick for %s", repo_root)
