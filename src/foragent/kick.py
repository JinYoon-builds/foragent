"""Wake the gate without delaying the operation that made work available."""

import logging
import os
import re
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
        # The latch is the wake itself: a pass running now finds it when it ends and
        # runs once more, even if the start below meets that pass or is ignored.
        (directory / LATCH).touch()
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
    plist = launchd.installed_plist(repo)
    # Kickstart only a job known to carry the gate environment; an older or
    # unreadable one gets the direct start, which sets it (launchd.refresh_if_stale).
    if not launchd.has_gate_environment(plist):
        return False
    target = f"gui/{os.getuid()}/{label}"
    try:
        # launchd accepts a kickstart of a running job without starting anything. That job
        # may be past its last latch check, so a direct start must take this wake instead.
        state = subprocess.run(["launchctl", "print", target],
                               capture_output=True, text=True, timeout=10, check=False)
        if state.returncode == 0 and _RUNNING.search(state.stdout or ""):
            return False
        result = subprocess.run(["launchctl", "kickstart", target],
                                capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


_RUNNING = re.compile(r"^\s*(state = running|pid = \d+)\s*$", re.MULTILINE)


# Written by every kick before it starts a pass; see kick_tick and take.
LATCH = "tick-kick.pending"
# Passes one gate process runs back to back for kicks; the timer covers anything beyond.
MAX_LATCHED_PASSES = 3


def wake(repo_root: Path) -> None:
    """Kick through the facade binding, so every caller shares one replaceable helper."""
    from foragent import orch

    orch.kick_tick(repo_root)


def take(store: StateStore) -> bool:
    """Spend the kick latch; True when a kick arrived since it was last spent.

    A pass spends it right after it takes the tick lock (it serves every earlier kick)
    and again after it releases the lock: True then means a kick met the pass, so the
    same process runs exactly one more pass, however many kicks arrived.
    """
    try:
        (store.root / LATCH).unlink()
    except FileNotFoundError:
        return False
    except OSError:
        logging.getLogger(__name__).exception("Could not read the tick kick latch in %s", store.root)
        return False
    return True
