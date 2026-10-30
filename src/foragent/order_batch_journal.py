"""The one in-flight batch write, so an interrupted flush is undone or finished from Git facts.

Before a flush writes anything it lists every path it will write (order files, notes and
new-lane files) with its earlier bytes and the base commit. New lanes are committed in
the batch commit itself. That commit is created first and its sha saved here before HEAD
moves to it (``GitRepo.commit_recorded``), so a commit HEAD holds is always recorded.
Order files and notes are committed from memory and written to the checkout only after
the commit, so before it nothing of them is in the shared checkout for anyone to pick up.
Whether the batch committed is that one fact:

- the recorded sha is an ancestor of HEAD: the batch is done; paths it had not written
  yet take HEAD's bytes, every listed path is unstaged to HEAD, and its checkpoint (orders
  and notes marked committed with that commit) is written;
- no sha is recorded, or it is not an ancestor: the batch did not land;
  every listed path is put back and unstaged, so nothing stays in the index: to
  HEAD's bytes when a commit since the base changed it (someone else's committed work),
  else to its bytes before the flush. Skill directories the flush created are removed.

Either way the journal is then removed and a later flush retries the remaining work.
"""

import base64
import json
import os
import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

from foragent import order, order_references
from foragent.i18n import text
from foragent.order_files import check_write, temporary_path, write_atomic
from foragent.paths import repo_path
from foragent.state_locks import _file_lock

JOURNAL_FILE = "order-batch-journal.json"
VERSION = 6


def _directory(root: Path) -> Path:
    """The ignored local control directory; a planted symlink must not redirect it."""
    directory = order_references.real_directory(root, ".foragent")
    ignore = directory / ".gitignore"
    if ignore.is_symlink():
        raise order.PlanError(f"{ignore} must be a regular file, not a symlink")
    if not ignore.exists() or ignore.read_bytes() == b"":
        write_atomic(root, ".foragent/.gitignore", b"*\n")  # never a half-written ignore rule
    return directory


@contextmanager
def _lock(root: Path, name: str):
    path = _directory(root) / name
    if path.is_symlink():
        raise order.PlanError(f"{path} must be a regular file, not a symlink")
    with _file_lock(path):
        yield


def _bytes(root: Path, name: str) -> bytes | None:
    path = repo_path(root, name)
    return path.read_bytes() if path.is_file() else None


def _encoded(data: bytes | None) -> str | None:
    return None if data is None else base64.b64encode(data).decode("ascii")


def _decoded(value: str | None) -> bytes | None:
    return None if value is None else base64.b64decode(value)


def lane_writes(root: Path, config, lanes: list[dict], manager=None) -> dict:
    """Every path the batch's lanes may write or commit, with its bytes now.

    New lanes get what ``add_lane`` writes; lanes already configured get the files their
    recovery commits, so both are restored and unstaged alike. The file list and skill
    directory names come from the lane setup itself (``lanes.lane_write_paths`` and the
    installed names ``validate_lane`` reports).
    """
    from foragent.lanes import lane_write_paths, validate_lane

    files, seen, created = set(), set(), {}
    for lane in lanes:
        if lane["name"] in seen:
            continue
        seen.add(lane["name"])
        if lane["name"] in config.lanes:
            files |= set(lane_write_paths(lane["name"], config.lanes[lane["name"]].get("skills", [])))
            continue
        installed = validate_lane(root, config, lane["name"], lane["paths"], lane["skills"], manager=manager)
        paths = lane_write_paths(lane["name"], installed)
        files |= set(paths)
        own = [f".claude/skills/{skill}" for skill in installed if not repo_path(root, f".claude/skills/{skill}").exists()]
        # What this lane depends on: kept or removed together with its configuration.
        created[lane["name"]] = {"files": [path for path in paths if path != ".foragent.yml"], "directories": own}
    return {"files": {name: _encoded(_bytes(root, name)) for name in sorted(files)}, "created": created}


def _save(directory: Path, value: dict) -> None:
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=directory, prefix=".order-batch-journal.",
                                     suffix=".tmp", delete=False) as stream:
        stream.write(json.dumps(value, ensure_ascii=False) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(stream.name, directory / JOURNAL_FILE)


def _load(directory: Path) -> dict:
    return json.loads((directory / JOURNAL_FILE).read_text(encoding="utf-8"))


def landed(git, sha: str | None) -> bool:
    """The one recovery predicate: a recorded commit that is an ancestor of HEAD."""
    return sha is not None and git.is_ancestor(sha, "HEAD")


def batch_done(directory: Path, sha: str) -> None:
    """Record the batch commit's sha; ``commit_recorded`` calls this before HEAD moves to it."""
    value = _load(directory)
    value["batch_commit"] = sha
    _save(directory, value)


def write(directory: Path, root: Path, files: dict[str, str | bytes], *, branch: str, base: str,
          orders: dict[str, list[str]], notes: list[str], lanes: dict | None = None) -> None:
    """List every path the flush will write, with its bytes now, before any of them changes."""
    _save(directory, {"version": VERSION, "branch": branch, "base": base, "orders": orders, "notes": notes,
                      "files": {name: _encoded(_bytes(root, name)) for name in sorted(files)},
                      "batch_commit": None, "lanes": lanes or {"files": {}, "created": {}}})


def clear(directory: Path) -> None:
    (directory / JOURNAL_FILE).unlink(missing_ok=True)


def _restore(root: Path, name: str, before: bytes | None) -> None:
    if _bytes(root, name) == before and not repo_path(root, name).is_symlink():
        return
    parent = PurePosixPath(name).parent.as_posix()
    check_write(root, name, parent)  # never through a planted link
    if before is None:
        repo_path(root, name).unlink()
    else:
        order_references.real_directory(root, parent)  # one real directory at a time
        check_write(root, name, parent)
        write_atomic(root, name, before)


def _targets(git, base: str, files: dict[str, str | None]) -> dict[str, bytes | None]:
    """The bytes each path goes back to: HEAD's when a commit since ``base`` changed it, else earlier."""
    names = list(files)
    then, now = (git.blob_shas(base, names), git.blob_shas("HEAD", names)) if names else ({}, {})
    return {name: (None if now[name] == "missing" else git.blob_bytes(now[name].encode()))
            if then[name] != now[name] else _decoded(encoded) for name, encoded in files.items()}


def _kept_lanes(configuration: bytes | None, created: dict) -> set[str]:
    """Lanes this batch created that the configuration surviving recovery still lists."""
    import yaml

    try:
        lanes = (yaml.safe_load(configuration.decode("utf-8")) or {}).get("lanes") if configuration else None
    except (ValueError, yaml.YAMLError, AttributeError):
        lanes = None
    return set(created) & set(lanes) if isinstance(lanes, dict) else set()


def _put_back(root: Path, git, base: str, files: dict[str, str | None], lanes: dict) -> None:
    """Undo a batch that is not in HEAD's history; work someone else committed since stays.

    A lane whose configuration survives (someone committed it) keeps everything it depends
    on as it is now: its agent, skill directories and the shared ignore and attribute
    lines. Every other created lane loses all of them, so the two never disagree.
    """
    targets = _targets(git, base, files)
    created = lanes.get("created", {})
    kept = _kept_lanes(targets.get(".foragent.yml", _bytes(root, ".foragent.yml")), created)
    keep = {path for name in kept for path in created[name]["files"]}
    for name, target in targets.items():
        if name not in keep:
            _restore(root, name, target)
    git.restore_staged_paths("HEAD", list(files))  # nothing of the batch stays staged
    _remove_directories(root, [directory for name, lane in created.items() if name not in kept
                               for directory in lane["directories"]])


def _finish(root: Path, git, files: dict[str, str | None], lane_paths: list[str]) -> None:
    """Complete a landed batch that stopped before writing the checkout or syncing the index.

    Order files and notes reach the checkout only after the commit, so a path still holding
    its recorded earlier bytes was never written: it takes HEAD's bytes. Every listed path,
    lane files included, is then unstaged to HEAD, so no reversal of the batch stays staged.
    """
    names = list(files)
    heads = git.blob_shas("HEAD", names) if names else {}
    for name, encoded in files.items():
        if _bytes(root, name) == _decoded(encoded):
            _restore(root, name, None if heads[name] == "missing" else git.blob_bytes(heads[name].encode()))
    git.restore_staged_paths("HEAD", [*names, *lane_paths])


def _remove_directories(root: Path, directories: list[str]) -> None:
    # Copied skills are ignored by Git, so a partial copy is removed, never trusted.
    for relative in directories:
        path = repo_path(root, relative)
        if path.is_symlink():
            raise order.PlanError(f"{relative} must be a real directory, not a symlink")
        if path.is_dir():
            shutil.rmtree(path)


def recover(directory: Path, root: Path, git, mark_committed, language: str) -> None:
    """Finish or undo a recorded write; ``mark_committed(orders, notes, blobs, commit)`` records success."""
    path = directory / JOURNAL_FILE
    if path.is_symlink():
        raise order.PlanError(f"{path} must be a regular file, not a symlink")
    try:
        value = _load(directory)
    except FileNotFoundError:
        return
    except ValueError as exc:
        raise order.PlanError(f"{path} is not valid JSON") from exc
    if value.get("version") != VERSION:
        raise order.PlanError(text("batch_recover_format", language, path=path))
    # Recovery needs no configuration: the branch it wrote on is recorded here.
    if git.current_branch() != value["branch"]:
        raise order.PlanError(text("batch_recover_branch", language, branch=value["branch"]))
    files, lanes = value["files"], value["lanes"]
    for name in [*files, *lanes["files"]]:
        leftover = temporary_path(root, name)
        if leftover.is_symlink() or leftover.is_file():
            leftover.unlink()  # an interrupted atomic write never reached its file
    commit = value["batch_commit"]
    if landed(git, commit):
        _finish(root, git, files, list(lanes["files"]))
        # Committed blobs come from that commit, so later commits cannot change them.
        mark_committed(value["orders"], value["notes"], git.blob_shas(commit, list(files)), commit)
        clear(directory)
        return
    _put_back(root, git, value["base"], {**lanes["files"], **files}, lanes)
    clear(directory)
