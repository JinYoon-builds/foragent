"""Publish one explicit boundary in one commit/push, then apply issue controls.

Waiting logs belong to planning. Only this module advances the flush journal.
Interrupted batches resume from their explicit intent and Git history.
"""

from pathlib import PurePosixPath
from uuid import uuid4

from foragent import order, order_flush_journal as journal, order_flush_recovery as recovery
from foragent import order_notes, order_references, order_reviewed
from foragent.git import GitError
from foragent.i18n import text
from foragent.order_files import check_destinations, check_write
from foragent.ownership import LABEL_COLOR, label_description, owner_label
from foragent.paths import repo_path
from foragent.tracker import LabelSpec, TrackerError


def _merge(files, incoming, language):
    for name, content in incoming.items():
        if name in files and files[name] != content:
            raise order.PlanError(text("order_flush_conflict", language, path=name))
        files[name] = content


def _note_files(root, notes, language):
    files = {}
    destinations = {"decision": "docs/decisions.md", "idea": "docs/ideas.md", "rule": "docs/rules.md"}
    for note in notes:
        name = destinations[note["kind"]]
        check_write(root, name, "docs")
        if name not in files:
            path = repo_path(root, name)
            files[name] = path.read_bytes().decode("utf-8") if path.exists() else ""
        previous = files[name]
        if note["kind"] == "rule":
            lines = previous.splitlines(keepends=True)
            old = note["replace"]
            matches = [i for i, line in enumerate(lines) if line.rstrip("\r\n") == old]
            if len(matches) != 1:
                raise order.PlanError(text("order_flush_rule_missing", language, id=note["id"]))
            index = matches[0]
            line = lines[index]
            ending = line[len(line.rstrip("\r\n")):]
            lines[index] = note["text"] + ending
            files[name] = "".join(lines)
        else:
            files[name] = previous + ("\n" if previous and not previous.endswith("\n") else "") + f"- {note['text']}\n"
    return files


def _preflight(rows, notes, root, config, tracker, repo, git):
    prepared, files, roots = [], {}, {}
    for row in rows:
        try:
            plan = row["plan"]
            publication = order._publication_checks(
                plan, root, config, tracker, repo=repo, approve=row["approve"],
                approve_keys=row["approve_keys"], approve_numbers=row["approve_numbers"],
                retained_files=row.get("files"), git=git, refreshed=True)
            _merge(files, publication.files, config.language)
        except (ValueError, OSError, TrackerError) as exc:
            raise order.PlanError(text("order_flush_refused", config.language, id=row["id"], error=exc)) from exc
        references = set(plan.get("reference_files", {}))
        roots.update({name: "docs/specs" if name == f"docs/specs/{plan['id']}.md"
                      else "docs/design" if name in references else config.acceptance_dir
                      for name in publication.files})
        prepared.append((row, publication))
    note_files = _note_files(root, notes, config.language)
    _merge(files, note_files, config.language)
    roots.update({name: "docs" for name in note_files})
    check_destinations(root, files)
    for name in files:
        check_write(root, name, roots[name])
    _refuse_affected(prepared, files, config, tracker, repo, git)
    return prepared, files, roots


def _refuse_affected(prepared, files, config, tracker, repo, git):
    # Compares with the tracking ref, which every caller has just fetched.
    own = set().union(*(publication.own for _row, publication in prepared))
    affected = order._affected_owners(tracker.list_issues(), own, files, repo, config, lambda: git, fetch=False)
    if affected:
        raise order.PlanError(text("order_locks_refused", config.language,
                                   owners="\n".join(order._owner_lines(affected))))


def _write(root, git, files, roots, title):
    check_destinations(root, files)
    for name in files:
        check_write(root, name, roots[name])
    for name, content in files.items():
        path = repo_path(root, name)
        data = content.encode("utf-8") if isinstance(content, str) else content
        if not path.exists() or path.read_bytes() != data:
            check_write(root, name, roots[name])
            order_references.real_directory(root, PurePosixPath(name).parent.as_posix())
            check_write(root, name, roots[name])
            path.write_bytes(data)
    if not files:
        return git.head_sha()
    git.ensure_tracked(list(files))
    changed = git.uncommitted_paths(list(files))
    if changed:
        git.commit_paths(changed, title)
    return git.head_sha()


def _members(store, batch):
    orders = [row for row in order_notes.entries(store, "orders.jsonl", with_offsets=True)
              if row["entry_id"] in batch["orders"]]
    notes = [row for row in order_notes.entries(store, with_offsets=True)
             if row["entry_id"] in batch["notes"]]
    latest = {row["id"]: row for row in orders}
    return [row for row in orders if row is latest[row["id"]]], notes


def _require_base(git, config):
    try:
        branch = git.current_branch()
    except GitError:
        branch = None
    if branch != config.base_branch:
        raise order.PlanError(text("order_flush_branch", config.language, branch=config.base_branch))


def _finish(root, config, tracker, git, store, repo, batch, records, prepared=None, sha=None):
    resuming = sha is None
    if resuming:
        sha = recovery.committed(root, config, git, store, batch, records)
        if sha is None:
            rows, notes = _members(store, batch)
            prepared, files, roots = _preflight(rows, notes, root, config, tracker, repo, git)
            if set(files) != set(batch["paths"]):
                raise order.PlanError(text("order_flush_paths_changed", config.language))
            sha = _write(root, git, files, roots, recovery.title(batch))
            journal.append(store, "committed", batch["batch"], sha=sha)
    completed = {row.get("entry", row.get("order")) for row in records
                 if row["batch"] == batch["batch"] and row["kind"] == "issues"}
    if prepared is None:
        rows, _notes = _members(store, batch)
        prepared = [(row, order._publication_checks(
            row["plan"], root, config, tracker, repo=repo, approve=row["approve"],
            approve_keys=row["approve_keys"], approve_numbers=row["approve_numbers"],
            retained_files=row.get("files"), landed=True)) for row in rows
            if row["entry_id"] not in completed and row["id"] not in completed]
        # An owner approved since the commit may lock bytes this retried push would move.
        if not git.is_ancestor(sha, f"origin/{config.base_branch}"):
            _refuse_affected(prepared, {}, config, tracker, repo, git)
    sha = recovery.push(config, git, store, batch, sha, recovering=resuming)
    for row, publication in prepared:
        if row["entry_id"] in completed or row["id"] in completed:
            continue
        owner = publication.owner
        tracker.ensure_labels([LabelSpec(owner_label(owner), LABEL_COLOR, label_description(owner))])
        order._publish_issues(row["plan"], root, config, tracker, repo=repo, publication=publication,
                              published_blobs=git.blob_shas(sha, publication.files),
                              approve=row["approve"])
        journal.append(store, "issues", batch["batch"], entry=row["entry_id"],
                       order=row["id"], mapping=publication.mapping)
    journal.append(store, "done", batch["batch"])
    return {"boundary": batch["boundary"], "batch": batch["batch"], "status": "published", "sha": sha}


def flush(root, config, tracker, git, store, *, repo, boundary):
    # Before lock creation or checkpoint reads: a wrong-branch invocation has no effects.
    _require_base(git, config)
    # The gate spans the checks below and every write, as in order.publish.
    with journal.locked(store), order_reviewed.publication_gate(repo):
        records = journal.entries(store)
        done = {row["batch"] for row in records if row["kind"] == "done"}
        pending = [row for row in records if row["kind"] == "selected" and row["batch"] not in done]
        result = {"boundary": boundary, "status": "empty"}
        if pending:
            with git.publish_lock():
                for batch in pending:
                    result = _finish(root, config, tracker, git, store, repo, batch, records)
            records = journal.entries(store)
        selected = journal.selected_entries(records)
        orders = [row for row in order_notes.entries(store, "orders.jsonl", with_offsets=True)
                  if row["boundary"] == boundary and row["entry_id"] not in selected]
        notes = [row for row in order_notes.entries(store, with_offsets=True)
                 if row["boundary"] == boundary and row["entry_id"] not in selected]
        if not orders and not notes:
            return result
        latest = {row["id"]: row for row in orders}
        effective = [row for row in orders if row is latest[row["id"]]]
        with git.publish_lock():
            git.update_base(config.base_branch)
            # Validate against the refreshed base before starting a durable batch.
            prepared, files, roots = _preflight(effective, notes, root, config, tracker, repo, git)
            batch = {"kind": "selected", "batch": uuid4().hex[:12], "boundary": boundary,
                     "orders": [row["entry_id"] for row in orders],
                     "notes": [row["entry_id"] for row in notes], "paths": sorted(files)}
            journal.append(store, **batch)
            sha = _write(root, git, files, roots, recovery.title(batch))
            journal.append(store, "committed", batch["batch"], sha=sha)
            return _finish(root, config, tracker, git, store, repo, batch, records, prepared, sha)


def run(args, root):
    from foragent.commands import facade, repo_store

    cli = facade()
    config, repo, store = repo_store(root)
    git = cli.GitRepo(root)
    # Constructing RunContext may persist tracker facts; refuse before constructing it.
    _require_base(git, config)
    tracker = cli.RunContext(repo=repo, repo_root=root, tracker=cli.GhTracker(repo), store=store,
                             config=config, git=git, now=lambda: cli.datetime.now(cli.UTC)).tracker
    try:
        result = flush(root, config, tracker, git, store, repo=repo, boundary=args.boundary)
    except (OSError, cli.GitError, cli.TrackerError, order.PlanError) as exc:
        raise order.PlanError(text("order_flush_failed", config.language,
                                   error=" ".join(str(exc).split()))) from exc
    print(cli.json.dumps(result, ensure_ascii=False) if args.json else
          text("order_flush_result", config.language, boundary=args.boundary,
               status=text(f"order_flush_status_{result['status']}", config.language)))
    return 0
