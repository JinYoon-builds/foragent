"""Build the merge-owner batch start state once per pytest process, then copy it.

Most batch tests start by running the real runners until every member PR is
queued. That start is the same whenever the test reaches it in the same state,
so the first test runs the real runners and keeps a snapshot: the disk state
under its own tmp_path (origin, clone, run state, measurements) and the data
the in-memory GitHub, CI, Git and store objects gained. A later test whose
state matches the first one's exactly copies that snapshot over its own
tmp_path (no Git runs) and gets the same data. Its own fixtures, patches and
closures stay in place.

A test that patched anything, changed any tracked data or added commits before
queueing has another fingerprint and runs the real runners. Set
FA_BATCH_START_CACHE=0 to run the real runners in every test.
"""

import copy
import dataclasses
import datetime
import enum
import hashlib
import os
from pathlib import Path
import re
import shutil
import tempfile
from types import SimpleNamespace

from conftest import git_command

_DIRS = ("origin.git", "clone", "state", "measurements")
_SNAPSHOTS = {}  # fingerprint -> (old root, data per object, case data); one per pytest process
_ADDRESS = re.compile(r" at 0x[0-9a-f]+")
_RANDOM_ID = re.compile(r"\b[0-9a-f]{32}\b")  # uuid4().hex run generations differ in every real run
# Per-test mkdtemp folders (an isolated home, for example) end in eight random characters.
_TEMP_NAME = re.compile(re.escape(os.path.realpath(tempfile.gettempdir())) + r"/([A-Za-z-]*?)[a-z0-9_]{8}\b")


def prepare(case, root, monkeypatch):
    """Remember where this case lives and which patches its fixtures made."""
    case.start_root = Path(root)
    case.start_patches = monkeypatch


def queued_once(case, numbers, run):
    """Run `run(case, numbers)` for real, or restore the identical start it produced earlier."""
    root = getattr(case, "start_root", None)
    if root is None or os.environ.get("FA_BATCH_START_CACHE") == "0" or getattr(case, "start_used", False):
        return run(case, numbers)
    case.start_used = True  # Only the first queue in a test starts from the shared world.
    if not _layout_matches(case, root):
        return run(case, numbers)
    key = _fingerprint(case, root, numbers)
    saved = _SNAPSHOTS.get(key)
    if saved is not None and _restore(case, root, saved):
        return None
    before = {name: (_data(obj), dict(vars(obj))) for name, obj in _objects(case).items()}
    result = run(case, numbers)
    _SNAPSHOTS.setdefault(key, _snapshot(case, root, before))
    return result


def _is_lock_owner(path):
    """A file_locks holder record, named `.{lock name}.owner` beside its lock."""
    return path.name.startswith(".") and path.name.endswith(".owner") and (path.parent / path.name[1:-6]).exists()


def _layout_matches(case, root):
    return (Path(case.history.origin) == root / "origin.git" and Path(case.root) == root / "clone"
            and Path(case.store.root) == root / "state" and Path(case.scratch) == root / "measurements")


def _objects(case):
    """Objects whose data the real runners change; the case itself is handled apart."""
    found = {"tracker": case.tracker, "projects": case.tracker._projects, "ci": case.ci,
             "git": case.git, "store": case.store, "ctx": case.ctx, "worker": case.worker}
    if hasattr(getattr(case.ctx, "reads", None), "__dict__"):
        found["reads"] = case.ctx.reads
    return found


_SCALARS = (type(None), bool, int, float, complex, str, bytes, Path, datetime.date, datetime.time,
            datetime.timedelta, datetime.tzinfo, enum.Enum)


def _is_data(value, seen=None):
    """Plain values only: no functions, live services or locks that would carry the first test's closures."""
    seen = set() if seen is None else seen
    if isinstance(value, _SCALARS):
        return True
    if id(value) in seen:
        return True
    seen.add(id(value))
    if isinstance(value, (list, tuple, set, frozenset)):
        return all(_is_data(item, seen) for item in value)
    if isinstance(value, dict):
        return all(_is_data(k, seen) and _is_data(v, seen) for k, v in value.items())
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return all(_is_data(getattr(value, f.name), seen) for f in dataclasses.fields(value))
    if type(value) is SimpleNamespace:
        return all(_is_data(item, seen) for item in vars(value).values())
    return False


def _data(obj):
    """An object's own non-callable attributes, deep-copied; None marks an uncopyable value."""
    found = {}
    for name, value in vars(obj).items():
        if callable(value) and not isinstance(value, (list, dict, set, tuple)):
            continue
        try:
            found[name] = copy.deepcopy(value)
        except Exception:
            found[name] = None
    return found


def _stable(value, root):
    text = repr(value)
    for form in {str(root), os.path.realpath(root)}:
        text = text.replace(form, "<tmp>")
    return _ADDRESS.sub("", text)


def _describe(value, root):
    """A function by its name and the values it closes over; anything else by its repr."""
    if callable(value) and hasattr(value, "__qualname__"):
        cells = []
        for cell in getattr(getattr(value, "__func__", value), "__closure__", None) or ():
            try:
                contents = cell.cell_contents
            except ValueError:
                contents = "<empty>"
            cells.append(_stable(contents, root) if not callable(contents) else getattr(contents, "__qualname__", ""))
        return value.__qualname__, cells
    return _stable(value, root)


def _fingerprint(case, root, numbers):
    """Everything that can steer the real runners, with this test's tmp root, addresses and random ids removed."""
    digest = hashlib.sha256()
    for part in _fingerprint_parts(case, root, numbers):
        digest.update(part.encode())
        digest.update(b"\0")
    return digest.hexdigest()


def _fingerprint_parts(case, root, numbers):
    parts = []

    def add(value):
        parts.append(_TEMP_NAME.sub(r"<temp>/\1", _RANDOM_ID.sub("<id>", _stable(value, root))))

    add(tuple(numbers))
    add(sorted((name, value) for name, value in vars(case).items()
               if not callable(value) and not hasattr(value, "__dict__")
               and name not in {"start_root", "start_patches", "start_used", "released",
                                "transport_lock", "measure_lock"}))
    add(case.config)
    for name, obj in _objects(case).items():
        add(name)
        add(sorted((k, _describe(v, root)) for k, v in vars(obj).items() if k not in {"case", "lock", "_lock"}))
    patches = case.start_patches
    add([(getattr(obj, "__name__", type(obj).__name__), attr, _describe(getattr(obj, attr, None), root))
         for obj, attr, _ in patches._setattr])
    add([(key, os.environ.get(key)) for _, key, _ in patches._setitem if isinstance(key, str)])
    add(git_command(case.history.origin, "for-each-ref", "--format=%(refname) %(objectname)"))
    add(git_command(case.root, "for-each-ref", "--format=%(refname) %(objectname)"))
    add(git_command(case.root, "rev-parse", "HEAD"))
    for directory in (root / "state", root / "measurements"):
        for path in sorted(directory.rglob("*")):
            if _is_lock_owner(path):
                continue  # Who held a lock and when (pid, wall clock); it steers nothing.
            add(path.relative_to(root).as_posix())
            if path.is_file():
                add(path.read_bytes().decode("utf-8", "replace"))
    return parts


def _snapshot(case, root, before):
    """Keep the disk start and every object datum the real runners changed, or None."""
    changed = {}
    for name, obj in _objects(case).items():
        copies, live = before[name]
        changed[name] = {}
        for attr, value in vars(obj).items():
            if not _is_data(value):
                if attr in live and live[attr] is value:
                    continue  # The same live object; its own data is tracked when it matters.
                return None  # A new function or service object: always run for real.
            if attr in copies and repr(copies[attr]) == repr(value):
                continue
            changed[name][attr] = value
    changed = copy.deepcopy(changed)  # One copy keeps references shared between objects shared.
    case_data = {name: copy.deepcopy(getattr(case, name)) for name in ("clock", "measurements", "model_calls")}
    keep = root / "batch-start-snapshot"
    shutil.rmtree(keep, ignore_errors=True)
    for name in _DIRS:
        shutil.copytree(root / name, keep / name, symlinks=True)
    return root, changed, case_data


def _rewrite(value, old, new, seen=None):
    """Rewrite the old tmp root to the new one inside strings and paths, keeping shared references shared."""
    seen = {} if seen is None else seen
    if isinstance(value, str):
        return value.replace(old, new)
    if isinstance(value, Path):
        return Path(str(value).replace(old, new))
    if isinstance(value, _SCALARS):
        return value
    if id(value) in seen:
        return seen[id(value)]
    if isinstance(value, list):
        out = seen[id(value)] = []
        out.extend(_rewrite(item, old, new, seen) for item in value)
    elif isinstance(value, dict):
        out = seen[id(value)] = {}
        out.update((_rewrite(k, old, new, seen), _rewrite(v, old, new, seen)) for k, v in value.items())
    elif isinstance(value, (tuple, set, frozenset)):
        out = seen[id(value)] = value.__class__(_rewrite(item, old, new, seen) for item in value)
    elif dataclasses.is_dataclass(value) and not isinstance(value, type):
        out = seen[id(value)] = dataclasses.replace(value, **{
            f.name: _rewrite(getattr(value, f.name), old, new, seen) for f in dataclasses.fields(value) if f.init})
    elif type(value) is SimpleNamespace:
        out = seen[id(value)] = SimpleNamespace()
        vars(out).update((k, _rewrite(v, old, new, seen)) for k, v in vars(value).items())
    else:
        out = value
    return out


def _assign(obj, attr, value):
    current = getattr(obj, attr, None)
    if type(current) is list and type(value) is list:
        current[:] = value  # Closures may hold the list itself.
    elif type(current) is dict and type(value) is dict:
        current.clear()
        current.update(value)
    else:
        setattr(obj, attr, value)


def _restore(case, root, saved):
    """Copy the snapshot onto this test's tmp_path and data; False leaves the test untouched."""
    old_root, changed, case_data = saved
    forms = sorted({(str(old_root), str(root)), (os.path.realpath(old_root), os.path.realpath(root))},
                   key=lambda pair: -len(pair[0]))
    data = copy.deepcopy((changed, case_data))
    for old, new in forms:
        data = _rewrite(data, old, new)  # Builds fresh containers, so the snapshot itself stays untouched.
    if any(old not in new and old in repr(data) for old, new in forms):
        return False
    keep = Path(old_root) / "batch-start-snapshot"
    if not keep.is_dir():
        return False
    for name in _DIRS:
        shutil.rmtree(root / name)
        shutil.copytree(keep / name, root / name, symlinks=True)
    for name in _DIRS:
        for path in (root / name).rglob("*"):
            if path.is_file() and "objects" not in path.relative_to(root / name).parts:
                raw = original = path.read_bytes()
                for old, new in forms:
                    raw = raw.replace(old.encode(), new.encode())
                if raw != original:
                    path.write_bytes(raw)
    changed, case_data = data
    objects = _objects(case)
    for name, attrs in changed.items():
        for attr, value in attrs.items():
            _assign(objects[name], attr, value)
    for name, value in case_data.items():
        _assign(case, name, value)
    return True
