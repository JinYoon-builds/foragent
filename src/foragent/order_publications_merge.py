"""Keep main's later changes when a queued order lands its file bytes (PM r1278).

A publication request carries the order's file bytes as they were on the commit its first
red check ran on (``baseline_red_check_commit``). When main changed one of those files
since, the order's own change is merged three-way onto main's current content: a change
to another part of the file survives beside the order's change, and an overlapping change
refuses the publication with a reason naming the file, so the order is prepared again.
Files main did not change keep exactly the order's bytes.
"""

from __future__ import annotations

from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory

from foragent import order
from foragent.git import GitError


class MainChanged(order.PlanError):
    pass


def keep_main(git, row, target, publication):
    """Rewrite ``publication.files`` in place onto ``target``'s content and return it."""
    if publication is None:
        return None  # The request was superseded during this pass.
    publication.files.update(merged(git, row.get("baseline_red_check_commit"), target, publication.files))
    return publication


def merged(git, base, target, files):
    """The files whose content main changed between ``base`` and ``target``, merged."""
    if not base or base == target:
        return {}
    names = [name for name, data in files.items() if isinstance(data, str)]
    before_blobs, now_blobs = git.blob_shas(base, names), git.blob_shas(target, names)
    result = {}
    for name in names:
        ours = files[name]
        if before_blobs[name] == now_blobs[name]:
            continue
        before, now = git.file_at(base, name), git.file_at(target, name)
        if now == ours:
            continue
        if before is None or now is None:
            raise MainChanged(_refusal(name, "main added or removed the file"))
        text = _merge3(before, ours, now)
        if text is None:
            raise MainChanged(_refusal(name, "main changed the same lines as the order"))
        result[name] = text
    return result


def _refusal(name, why):
    return (f"Main changed {name} after this order was prepared and {why}; "
            f"prepare the order again on the current main.")


def _merge3(base, ours, theirs):
    with TemporaryDirectory(prefix="order-merge-") as directory:
        paths = []
        for label, data in (("order", ours), ("prepared", base), ("main", theirs)):
            path = Path(directory) / label
            path.write_bytes(data.encode("utf-8"))
            paths.append(str(path))
        try:
            done = subprocess.run(["git", "merge-file", "-p", *paths], capture_output=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise GitError(str(exc)) from exc
    if done.returncode == 0:
        return done.stdout.decode("utf-8")
    if 0 < done.returncode < 128:
        return None  # Conflicts.
    raise GitError(done.stderr.decode("utf-8", "replace") or "git merge-file failed")
