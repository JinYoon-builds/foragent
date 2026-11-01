"""Publish one explicit boundary in one commit/push, then apply issue controls.

Waiting logs belong to planning. Only this module advances the flush journal.
Interrupted batches deliberately refuse here; their recovery is a separate issue.
"""

from pathlib import PurePosixPath
from uuid import uuid4

from foragent import order, order_flush_journal as journal, order_notes, order_references
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
    return prepared, files, roots


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
    git.ensure_tracked(list(files))
    changed = git.uncommitted_paths(list(files))
    if changed:
        git.commit_paths(changed, title)
    return git.head_sha()


def flush(root, config, tracker, git, store, *, repo, boundary):
    language = config.language
    # Before lock creation or checkpoint reads: a wrong-branch invocation has no effects.
    if git.current_branch() != config.base_branch:
        raise order.PlanError(text("order_flush_branch", language, branch=config.base_branch))
    with journal.locked(store):
        records = journal.entries(store)
        done = {row["batch"] for row in records if row["kind"] == "done"}
        if any(row["kind"] == "selected" and row["batch"] not in done for row in records):
            raise order.PlanError(text("order_flush_incomplete", language))
        selected = journal.selected_entries(records)
        orders = [row for row in order_notes.entries(store, "orders.jsonl", with_offsets=True)
                  if row["boundary"] == boundary and row["entry_id"] not in selected]
        notes = [row for row in order_notes.entries(store, with_offsets=True)
                 if row["boundary"] == boundary and row["entry_id"] not in selected]
        if not orders and not notes:
            return {"boundary": boundary, "status": "empty"}
        latest = {row["id"]: row for row in orders}
        # All selected revisions are checkpointed; only the latest of each order is applied.
        effective = [row for row in orders if row is latest[row["id"]]]
        with git.publish_lock():
            git.update_base(config.base_branch)
            # Validate against the refreshed base before starting a durable batch.
            prepared, files, roots = _preflight(effective, notes, root, config, tracker, repo, git)
            own = set().union(*(publication.own for _row, publication in prepared))
            affected = order._affected_owners(tracker.list_issues(), own, files, repo, config,
                                              lambda: git, fetch=False)
            if affected:
                raise order.PlanError(text("order_locks_refused", language,
                                           owners="\n".join(order._owner_lines(affected))))
            batch = uuid4().hex[:12]
            journal.append(store, "selected", batch, boundary=boundary,
                           orders=[row["entry_id"] for row in orders],
                           notes=[row["entry_id"] for row in notes], paths=sorted(files))
            sha = _write(root, git, files, roots, f"Foragent batch {boundary} {batch}")
            journal.append(store, "committed", batch, sha=sha)
            git.push_base(config.base_branch)
            journal.append(store, "pushed", batch)
        for row, publication in prepared:
            owner = publication.owner
            tracker.ensure_labels([LabelSpec(owner_label(owner), LABEL_COLOR, label_description(owner))])
            order._publish_issues(row["plan"], root, config, tracker, repo=repo, publication=publication,
                                  published_blobs=git.blob_shas(sha, publication.files),
                                  approve=row["approve"])
            journal.append(store, "issues", batch, order=row["id"], mapping=publication.mapping)
        journal.append(store, "done", batch)
        return {"boundary": boundary, "batch": batch, "status": "published", "sha": sha}


def run(args, root):
    from foragent.commands import facade, repo_store

    cli = facade()
    config, repo, store = repo_store(root)
    git = cli.GitRepo(root)
    # Constructing RunContext may persist tracker facts; refuse before constructing it.
    if git.current_branch() != config.base_branch:
        raise order.PlanError(text("order_flush_branch", config.language, branch=config.base_branch))
    tracker = cli.RunContext(repo=repo, repo_root=root, tracker=cli.GhTracker(repo), store=store,
                             config=config, git=git, now=lambda: cli.datetime.now(cli.UTC)).tracker
    try:
        result = flush(root, config, tracker, git, store, repo=repo, boundary=args.boundary)
    except (OSError, cli.GitError, cli.TrackerError, order.PlanError) as exc:
        raise order.PlanError(text("order_flush_failed", config.language, error=exc)) from exc
    print(cli.json.dumps(result, ensure_ascii=False) if args.json else
          text("order_flush_result", config.language, boundary=args.boundary,
               status=text(f"order_flush_status_{result['status']}", config.language)))
    return 0
