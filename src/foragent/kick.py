"""Wake the gate without delaying the operation that made work available."""

import logging
import os
from pathlib import Path
import subprocess
import sys

from foragent.config import load_config
from foragent.git import GitRepo
from foragent.launcher import gate_environment
from foragent.state import StateStore


def kick_tick(repo_root: Path) -> None:
    """Start a detached tick; its output and launch failures stay in kick.log."""
    try:
        root = Path(repo_root).resolve()
        repo = load_config(root).repo or GitRepo(root).remote_slug()
        directory = StateStore.for_repo(repo).root
        directory.mkdir(parents=True, exist_ok=True)
        if _launchd_kickstart(repo):
            return
        with (directory / "kick.log").open("ab") as stream:
            try:
                subprocess.Popen(
                    [sys.executable, "-m", "foragent.cli", "gate", "tick", "--repo-root", str(root)],
                    cwd=root, stdin=subprocess.DEVNULL, start_new_session=True,
                    stdout=stream, stderr=stream, env=gate_environment(),
                )
            except Exception as exc:
                stream.write(f"foragent: tick kick failed: {' '.join(str(exc).split())}\n".encode("utf-8"))
    except Exception:
        logging.getLogger(__name__).exception("Could not kick tick for %s", repo_root)


def _launchd_kickstart(repo: str) -> bool:
    """Start the installed gate job through launchd; True when launchd accepted it.

    A tick spawned directly inherits the caller's macOS QoS clamp, which no process
    can lift for itself. A launchd start runs with the job's own ProcessType.
    """
    if sys.platform != "darwin":
        return False
    from foragent import launchd

    label = launchd.label_for(repo)
    if not (Path.home() / "Library/LaunchAgents" / f"{label}.plist").is_file():
        return False
    try:
        result = subprocess.run(["launchctl", "kickstart", f"gui/{os.getuid()}/{label}"],
                                capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0
