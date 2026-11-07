"""Durable main writes, serialized by the merge owner.

Requests retain bytes outside the checkout. Git's three-way merge handles file
changes; a receipt in main recovers a push whose response or checkpoint was lost.
The queue envelope also accepts deterministic operations (for example notes).
A request whose payload sets "alone" is published as its own commit.
Writes disjoint from train members and CI inputs may land while a batch is open.
"""

from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
import base64
import json
import os
import subprocess
from uuid import uuid4

from foragent.git import GitError, GitRepo, main_write_lock
from foragent.run_base_move import _git_bytes

TRAILER = "Foragent-Main-Write"
IDENTITY = ["-c", "user.name=foragent", "-c", "user.email=foragent@localhost"]
TERMINAL_STATUSES = {"done", "retired"}


class Conflict(ValueError):
    """A deterministic operation or three-way merge cannot preserve both edits."""


def store_for(git, config):
    from foragent.state import StateStore

    try:
        repo = config.repo or git.remote_slug()
    except GitError:
        repo = "local" + git.root.resolve().as_posix()
    return StateStore.for_repo(repo)


def requests(store):
    return sorted((json.loads(path.read_text()) for path in (store.root / "main-writes").glob("*.json")),
                  key=lambda row: row["sequence"])


def pending(store):
    return [row for row in requests(store) if row["status"] not in TERMINAL_STATUSES]


def summary(store):
    return [{key: row.get(key) for key in ("id", "sequence", "target", "title", "requester", "status", "error")}
            for row in pending(store)]


def _save(store, row):
    # Escaped JSON also preserves POSIX names containing non-UTF-8 bytes.
    store._atomic_write(store.root / "main-writes" / f"{row['id']}.json",
                        json.dumps(row, ensure_ascii=True, indent=2) + "\n")


def submit(store, target, title, requester, payload, *, identity=None):
    with store._state_lock():
        return _submit(store, target, title, requester, payload, identity=identity)


def _submit(store, target, title, requester, payload, *, identity=None):
    rows = requests(store)
    identity = sha256(identity.encode()).hexdigest() if identity else uuid4().hex
    if existing := next((row for row in rows if row["id"] == identity), None):
        return existing
    row = dict(id=identity, sequence=max((r["sequence"] for r in rows), default=0) + 1,
               target=target, title=title, requester=requester, payload=payload, status="pending")
    _save(store, row)
    return row


def _content(data, mode="100644"):
    return {"mode": mode, "bytes": base64.b64encode(data).decode("ascii")}


def _tracked(git, paths, ref="HEAD"):
    entries = _git_bytes(git, ["--literal-pathspecs", "ls-tree", "-rz", ref, "--", *paths])
    result = {}
    for entry in filter(None, entries.decode("utf-8", "surrogateescape").split("\0")):
        meta, name = entry.split("\t", 1)
        mode, kind, blob = meta.split()
        if kind != "blob":
            raise GitError(f"main write cannot capture {kind}: {name}")
        result[name] = _content(git.blob_bytes(blob.encode()), mode)
    return result


def capture(git, config, paths, title, requester, *, store=None, base=None, snapshot=False):
    """Capture owned paths, including ignored files and deletions, without staging.

    Outstanding requests remain the producer's baseline across HEAD changes. This
    makes repeated ticks no-ops and consecutive edits incremental while main waits.
    An explicit base also retains local commits that have not reached main.
    A snapshot request carries every file under `paths`, removals included, so a
    later snapshot covers each file of an earlier one; it is queued only on change.
    """
    store = store or store_for(git, config)
    with store._state_lock():
        # Read the snapshot and its queued baseline in the same serialization boundary.
        base = base or git.head_sha()
        before = _tracked(git, paths, base)
        names = set(before)
        for name in paths:
            path = git.root / name
            if path.is_dir() and not path.is_symlink():
                names.update(p.relative_to(git.root).as_posix() for p in path.rglob("*")
                             if p.is_file() or p.is_symlink())
            else:
                names.add(name)
        after = {}
        for name in sorted(names):
            path = git.root / name
            if path.is_symlink():
                after[name] = _content(os.fsencode(os.readlink(path)), "120000")
            elif path.is_file():
                after[name] = _content(path.read_bytes(), "100755" if path.stat().st_mode & 0o111 else "100644")
            elif name in before:
                after[name] = None
        source = str(git.root.resolve())
        previous = {}
        for row in requests(store):
            if (row["target"] == config.base_branch and row["payload"].get("source") == source
                    and (row["status"] not in TERMINAL_STATUSES or row["payload"].get("base") == base)):
                previous.update({name: change["after"] for name, change in row["payload"].get("files", {}).items()})
        # A file first created in a queued request may have been removed again
        # before it ever appears in HEAD or the filesystem scan above.
        for name in previous:
            if any(name == path or name.startswith(path.rstrip("/") + "/") for path in paths):
                after.setdefault(name, None)
        files = {}
        for name, content in after.items():
            old = previous.get(name, before.get(name))
            if content != old:
                files[name] = {"before": old, "after": content}
        if not files:
            return None
        payload = {"kind": "files", "source": source, "base": base, "files": files}
        if snapshot:
            payload.update(snapshot=True, files={name: {"before": previous.get(name, before.get(name)),
                                                        "after": content}
                                                 for name, content in after.items()})
        return _submit(store, config.base_branch, title, requester, payload)


@contextmanager
def _index(git, directory):
    environment = {**os.environ, "GIT_INDEX_FILE": str(Path(directory) / "index")}

    def run(args):
        return subprocess.run(["git", *args], cwd=git.root, env=environment, check=True,
                              capture_output=True, text=True, errors="surrogateescape", timeout=120).stdout

    yield GitRepo(git.root, run=run)


def _tree(git, target, files, side, directory):
    with _index(git, directory) as index:
        index._call(["read-tree", target])
        for name, change in files.items():
            # Only index entries are written. Keep literal filename bytes, even
            # backslashes and symlink names, while refusing paths outside Git.
            path = PurePosixPath(name)
            if not name or "\0" in name or path.is_absolute() or ".." in path.parts or ".git" in path.parts:
                raise Conflict(f"Invalid main write path: {name!r}")
            content = change[side]
            if content is None:
                index._call(["update-index", "--force-remove", "--", name])
            else:
                blob = Path(directory) / "blob"
                blob.write_bytes(base64.b64decode(content["bytes"]))
                sha = index._call(["hash-object", "-w", "--", str(blob)]).strip()
                index._call(["update-index", "--add", "--cacheinfo", content["mode"], sha, name])
        return index._call(["write-tree"]).strip()


def _receipt(git, target, identity):
    marker = f"{TRAILER}: {identity}"
    candidates = git._call(["log", "--format=%H", "--fixed-strings", f"--grep={marker}", target]).splitlines()
    return next((sha for sha in candidates
                 if marker in git._call(["show", "-s", "--format=%B", sha]).splitlines()), None)


def _operation(ctx, row, target):
    payload = row["payload"]
    if payload["kind"] == "files":
        return payload["files"]
    if payload["kind"] == "close-locks":
        from foragent.orch_close import _merged_issues

        paths = dict(payload["paths"])
        for n in _merged_issues(ctx):
            approval = ctx.store.load_approval(n)
            if approval:
                for name in approval.acceptance_blobs:
                    paths.pop(name, None)
        files = {}
        for name, expected in paths.items():
            blob = ctx.git.blob_sha(target, name)
            if blob is None:
                continue
            if blob != expected:
                raise Conflict(f"{name} changed since issue #{payload['n']} was approved")
            files[name] = {"before": _tracked(ctx.git, [name], target)[name], "after": None}
        return files
    if payload["kind"] == "notes":
        from foragent.order_flush import _note_files

        names = ["docs/decisions.md", "docs/ideas.md", "docs/rules.md"]
        with TemporaryDirectory(prefix="main-notes-", dir=ctx.store.root) as directory:
            root = Path(directory)
            before = {}
            for name in names:
                sha = ctx.git.blob_sha(target, name)
                if sha is not None:
                    data = ctx.git.blob_bytes(sha.encode())
                    path = root / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(data)
                    before[name] = _content(data)
            try:
                return {name: {"before": before.get(name), "after": _content(data.encode())}
                        for name, data in _note_files(root, payload["notes"], ctx.config.language).items()}
            except ValueError as exc:
                raise Conflict(str(exc)) from exc
    raise ValueError(f"Unknown main write operation: {payload['kind']}")


def _merge_dream(ctx, row):
    from foragent.orch import merge

    payload = row["payload"]
    pr = ctx.tracker.get_pr(payload["pr"])
    if ctx.tracker.pr_head_sha(pr.number) != payload["head"] or pr.base != row["target"]:
        raise Conflict("Dream PR head or target changed")
    if not pr.merged_at:
        # Readiness refusals are retryable. Only a changed queued identity is a conflict.
        merge(ctx, pr.number, approved_by=payload["approved_by"], at=payload["at"], owner_request=True)
        ctx.git.fetch("origin", row["target"])
    return ctx.git.ref_sha(f"origin/{row['target']}")


def _merge_files(ctx, row, tip, files):
    """Three-way merge one request onto `tip`; return an unpublished commit of the result."""
    with TemporaryDirectory(prefix="main-write-", dir=ctx.store.root) as directory:
        if row.get("supersedes"):
            # It replaces blocked snapshots: main receives exactly this snapshot's files.
            tree = _tree(ctx.git, tip, files, "after", directory)
            return ctx.git._call([*IDENTITY, "commit-tree", tree, "-p", tip, "-m", row["title"]]).strip()
        before = _tree(ctx.git, tip, files, "before", directory)
        after = _tree(ctx.git, tip, files, "after", directory)
        parent = ctx.git._call([*IDENTITY, "commit-tree", before, "-m", "Main write base"]).strip()
        snapshot = ctx.git._call([*IDENTITY, "commit-tree", after, "-p", parent, "-m", row["title"]]).strip()
        try:
            tree = ctx.git._call(["merge-tree", "--write-tree", f"--merge-base={parent}", tip, snapshot]).splitlines()[0]
        except GitError as exc:
            if getattr(exc.__cause__, "returncode", None) == 1:
                raise Conflict(str(exc)) from exc
            raise
    return ctx.git._call([*IDENTITY, "commit-tree", tree, "-p", tip, "-m", row["title"]]).strip()


def _message(rows):
    trailers = "\n".join(f"{TRAILER}: {row['id']}" for row in rows)
    if len(rows) == 1:
        return f"{rows[0]['title']}\n\n{trailers}"
    listing = "\n".join(f"- {row['title']} ({row['requester']})" for row in rows)
    return f"Main writes: {len(rows)} requests\n\n{listing}\n\n{trailers}"


def _publish(ctx, rows, target, tip):
    """Land the merged result of `rows` as one commit on `target` with one push."""
    tree = ctx.git._call(["rev-parse", f"{tip}^{{tree}}"]).strip()
    commit = ctx.git._call([*IDENTITY, "commit-tree", tree, "-p", target, "-m", _message(rows)]).strip()
    for row in rows:
        row["applying"] = commit
        _save(ctx.store, row)
    branch = rows[0]["target"]
    ctx.git._call(["push", f"--force-with-lease=refs/heads/{branch}:{target}",
                   "origin", f"{commit}:refs/heads/{branch}"])
    return commit


def _landed(ctx, row, target):
    landed = _receipt(ctx.git, target, row["id"])
    if landed is not None or row["payload"]["kind"] != "notes":
        return landed
    from foragent import order_flush_journal as journal, order_flush_recovery as recovery

    # Direct flushes and owner writes share the batch identity, even when the
    # direct commit predates main-write receipts or its done checkpoint was lost.
    records = [record for record in journal.entries(ctx.store)
               if record["batch"] == row["payload"]["batch"]]
    if any(record["kind"] == "done" for record in records):
        return target
    batch = next((record for record in records if record["kind"] == "selected"), None)
    return recovery._find(ctx.git, target, batch) if batch else None


def _finish(ctx, row):
    if row["payload"]["kind"] == "notes":
        from foragent import order_flush_journal as journal

        with journal.locked(ctx.store):
            if not any(r["kind"] == "done" and r["batch"] == row["payload"]["batch"]
                       for r in journal.entries(ctx.store)):
                journal.append(ctx.store, "done", row["payload"]["batch"])


def _report_conflict(ctx, row):
    from foragent import orch_inbox

    if not any(item.get("main_write") == row["id"] for item in orch_inbox.requests(ctx.store)):
        orch_inbox.add_request(ctx.store, "merge-owner",
                               f"Main write for {row['requester']} conflicts with main: {row['error']}. "
                               f"Content retained in main-writes/{row['id']}.json",
                               extra={"main_write": row["id"], "requester": row["requester"]})


def _done(ctx, report, row, landed):
    _finish(ctx, row)
    row.update(status="done", landed=landed)
    _save(ctx.store, row)
    report.actions.append(f"main write {row['id']} ({row['requester']}): applied")


def _held(ctx, report, row, exc):
    # Network and lease failures stay pending; reconcile before retrying.
    row.update(status="pending", error=str(exc))
    _save(ctx.store, row)
    report.actions.append(f"main write {row['id']} ({row['requester']}): pending: {exc}")


def _blocked(ctx, report, row, exc):
    row.update(status="blocked", error=str(exc))
    _save(ctx.store, row)
    report.actions.append(f"main write {row['id']} ({row['requester']}): blocked: {exc}")


def _drain_dream(ctx, report, row):
    """Dream merging retains its own merge + main-write lock boundary."""
    try:
        ctx.git.fetch("origin", row["target"])
        target = ctx.git.ref_sha(f"origin/{row['target']}")
        landed = _landed(ctx, row, target)
        if landed is None:
            from foragent.main_write import train_in_flight

            if train_in_flight(ctx.git):
                return False
            row.pop("applying", None)
            landed = _merge_dream(ctx, row)
    except Conflict as exc:
        _blocked(ctx, report, row, exc)
        _report_conflict(ctx, row)
        return False
    except (GitError, OSError, ValueError) as exc:
        _held(ctx, report, row, exc)
        return False
    _done(ctx, report, row, landed)
    return True


def _drain_files(ctx, report, rows):
    """Land consecutive deterministic requests as one commit with one push.

    Each request merges onto the result of the requests before it. A conflicting
    request is blocked and reported on its own; the rest still share the commit.
    Returns False when the pass must stop to preserve FIFO order.
    """
    from foragent.main_write import changed_inputs
    from foragent.merge_owner import may_write_target

    landed, included, conflicts, proceed = [], [], [], True
    current = None
    try:
        with ctx.store.merge_lock(), main_write_lock(ctx.git):
            ctx.git.fetch("origin", rows[0]["target"])
            target = ctx.git.ref_sha(f"origin/{rows[0]['target']}")
            tip = target
            for row in rows:
                current = row
                if (receipt := _landed(ctx, row, target)) is not None:
                    landed.append((row, receipt))
                    continue
                try:
                    files = _operation(ctx, row, tip)
                    proposed = _merge_files(ctx, row, tip, files)
                    if not may_write_target(ctx, row["target"], changed_inputs(ctx.git, tip, proposed)):
                        proceed = False
                        break
                    row.pop("applying", None)
                    tip = proposed
                    included.append(row)
                except Conflict as exc:
                    _blocked(ctx, report, row, exc)
                    conflicts.append(row)
            current = None
            if included:
                commit = _publish(ctx, included, target, tip)
                landed.extend((row, commit) for row in included)
    except (GitError, OSError, ValueError) as exc:
        for row in [current] if current is not None else included:
            _held(ctx, report, row, exc)
        landed = [(row, receipt) for row, receipt in landed if row not in included]
        proceed = False
    # Flush takes its journal lock before the main-write lock. Release Git
    # locks before recording completion to keep that order acyclic.
    for row, receipt in sorted(landed, key=lambda item: item[0]["sequence"]):
        _done(ctx, report, row, receipt)
    for row in conflicts:
        _report_conflict(ctx, row)
    return proceed


def _supersede(ctx, report, row, rest):
    """Retire a blocked write that a later pending snapshot of the same requester covers.

    The later request must be a full snapshot naming every file of the blocked
    one, so the blocked write is never applied and nothing of it is needed.
    The later snapshot then lands its own content. Returns False when the queue
    must still stop.
    """
    if row["payload"].get("kind") != "files":
        return False
    names = set(row["payload"]["files"])
    later = next((r for r in rest if r["status"] == "pending" and r["payload"].get("snapshot")
                  and r["requester"] == row["requester"] and r["target"] == row["target"]
                  and r["payload"].get("source") == row["payload"].get("source")
                  and names <= set(r["payload"]["files"])), None)
    if later is None:
        return False
    reason = f"Superseded by later snapshot {later['id']}"
    with ctx.store._state_lock():
        later["supersedes"] = [*later.get("supersedes", []), row["id"]]
        _save(ctx.store, later)
        row.update(status="retired", reason=reason)
        _save(ctx.store, row)
    report.actions.append(f"main write {row['id']} ({row['requester']}): retired: {reason}")
    return True


def drain(ctx, report):
    """Called under merge-owner.lock, before preparing a new batch.

    One pass lands every applicable deterministic request in one main commit.
    """
    rows = pending(ctx.store)
    index = 0
    while index < len(rows):
        row = rows[index]
        # Closure withdraws the merge request, including one already blocked
        # by a changed identity. Reconcile it before retrying or enforcing FIFO.
        if row["payload"]["kind"] == "dream-merge":
            try:
                pr = ctx.tracker.get_pr(row["payload"]["pr"])
            except (GitError, OSError, ValueError) as exc:
                _held(ctx, report, row, exc)
                return
            if pr.state == "closed" and not pr.merged_at:
                reason = f"Dream PR #{pr.number} closed without merging"
                row.update(status="retired", reason=reason)
                _save(ctx.store, row)
                report.actions.append(f"main write {row['id']} ({row['requester']}): retired: {reason}")
                index += 1
                continue
        if row["status"] == "blocked":
            if _supersede(ctx, report, row, rows[index + 1:]):
                index += 1
                continue
            _report_conflict(ctx, row)
            return  # Preserve FIFO until the orchestrator resolves the conflict.
        if row["payload"]["kind"] == "dream-merge":
            if not _drain_dream(ctx, report, row):
                return
            index += 1
            continue
        end = index + 1
        # A request marked "alone" lands as its own commit; it also ends the group before it.
        while (not row["payload"].get("alone") and end < len(rows)
               and rows[end]["payload"]["kind"] != "dream-merge" and not rows[end]["payload"].get("alone")
               and rows[end]["status"] != "blocked" and rows[end]["target"] == row["target"]):
            end += 1
        if not _drain_files(ctx, report, rows[index:end]):
            return
        index = end
