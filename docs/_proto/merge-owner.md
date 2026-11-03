# Independent merge owner (prototype #1502)

Prototype record from `pm/merge-owner-proto` (`a053ec2e`). Current-main integration,
updated read counts and verification are in the [port report](merge-owner-port-report.md).

`merge_owner: true` opts in; the default is false. This remains an uninstalled
prototype. Every reported finding is a tool defect: lock ownership, resource
identity, durable state transitions, transport typing, read reuse or accounting.
The agent already had the relevant decision/evidence. No judgment classifier,
keyword rule, similarity threshold or agent step order was added.

## Ownership and process choice

The gate starts **one independent process for one owner pass**, using the existing
baseline builder's inherited-file-descriptor lock pattern. It reserves
`merge-owner.lock` without waiting, passes its descriptor and a configuration
snapshot to `python -m foragent.merge_owner`, then closes its own descriptor.
It never waits for that process. The child does not inherit `tick.lock`.

This choice keeps the existing verification functions intact: Done when,
local_only, related tests, local comparison, Git fetches and waiting for a test
slot can all take minutes. Moving the entire pass out of the tick is smaller and
more reliable than turning each of those shared functions into a polling state
machine. Worker admission and later scheduler ticks continue during that work.
Owner publication remains serial; a slow check can delay other owner merges,
but cannot retain the scheduling lock. This does not claim concurrent merging.

The lock reservation survives the launcher exiting and prevents overlapping
owner processes, including the gap before the child imports Python modules.
The child loads committed settings after its fetch when the gate uses committed
settings. It reaps previously recorded check identities under its own lock
before honoring GitHub backoff. If that cleanup is refused, only the affected
admission waits. Process tests use fake process tables for all signals.
Disabling owner mode starts no owner or cleanup process: drain first.

| Fact | Writer / authority |
| --- | --- |
| Implementation, independent review, Done when and machine evidence | Existing worker verification |
| Immutable publication admission | Worker ready row in merge-queue.jsonl |
| Worker lifecycle / worker slot | Existing run store and crash recovery |
| Publication progress, checked candidate, push and repair intent | Independent owner, merge-owner/ISSUE.json |
| Owner exclusivity | Inherited merge-owner.lock descriptor |
| Final publication exclusivity | Existing merge.lock |
| Current PR number, target, head, merged timestamp | GitHub PR resource |
| Completion | Validated owner_merge fact on the admitted run generation |
| Repair advice and readiness | Existing retry transition through the single close boundary |

## Structural boundaries

1. **Handoff and authorization.** The worker completes its verification and lost
   test comparison before durable admission. It returns merge-queued and frees
   its worktree/slot. orch.merge records PR number, target, authorized head,
   author and time. Review-only reads that exact PR; it cannot choose another PR
   on the same branch. Admission validates the same PR and target. Only a clean,
   independently reverified refresh can carry authorization to a descendant.
2. **One destructive tool boundary.** close_for_repair is the only owner-mode
   function that closes a PR. It checks the expected PR, branch, head and target
   and preserves its branch. Automatic retry, inspect retry, dream cleanup and
   temporary group cleanup all route through it when opted in. Owner merges
   also preserve branches. Owner mode has no branch deletion path. A second
   head read cannot make GitHub's close atomic, so preserving the remote ref
   closes the destructive race instead of pretending the read is a lease.
3. **Replayable repair.** The owner journals repair intent before retry advice,
   closure or label writes. Reconciliation replays repair before treating a
   closed PR as terminal. A matching already-closed PR skips the close and
   completes the retry marker, READY addition and BLOCKED/NEEDS_HUMAN removal.
   A manually closed PR with no saved repair intent still retires normally.
4. **One merge fact.** A successful merge response is not completion. The owner
   reads the actual merged PR and validates its number, branch, target and head
   against the admission. A manually merged PR goes through the same function.
   A mismatch holds and retires the admission without closing the issue,
   releasing acceptance ownership or reinstalling. The validated PR, target,
   head and GitHub merged_at are saved as owner_merge on that run generation;
   history records the same fact even after the worker finished. merge_times
   projects its timestamp, and run_merged reads its completion independently of
   worker outcome. Scorecard, ownership, prerequisites, recovery, watch, role
   activity, dreams, tidy and benchmark readers recognize that fact. Only a
   confirmed configured-base merge runs the reinstall hook.
5. **Bounded reads, not repeated polling.** Each owner pass uses a read snapshot.
   The GitHub adapter shares raw GET responses across nested CI helpers, including
   the PR/head and changed-file reads. Advancement reuses inputs.status.
   No process waits for CI or artifact registration. A fresh snapshot under
   merge.lock retains the mandatory final mutable-input recheck. Repair and
   post-merge confirmation also start fresh snapshots; writes/pushes invalidate
   observations that they change. Reads across these mutation/lock boundaries
   are deliberately fresh, rather than being suppressed by a pass-wide cache.
6. **Typed evidence failure.** The owner-only adapter returns RejectedEvidence
   for a completed unsuccessful producer or invalid artifact. Validation records
   a permanent hold (or uses an explicitly configured local fallback). Transport
   TrackerError retains the admission for retry. Missing/stale evidence waits
   without spending a worker or a queue deadline. The ordinary waiting adapter
   keeps its original TrackerError and fallback behavior.
7. **Recovery isolation.** An admitted worker's death finishes its run without
   revoking publication. A label transport failure is reported and does not
   abort the tick. Owner reconciliation retries stale worker labels from the
   durable admission, independently of other candidates and new launches.

## Flag-off audit

The comparison is against 232cd955, before this prototype, as well as this run's
starting HEAD c0afd4c7. [flag-off-audit.md](merge-owner-flag-off-audit.md) lists every
modified source hunk's current line spans and its guard/compatibility disposition.
The earlier globally changed paths were removed or scoped:

- _trusted_sha again returns None for a non-rate-limit lookup error unless the
  opt-in owner explicitly requests strict transport handling. Waiting workers
  retain their configured local fallback, including a default-branch HTTP 502.
- ComparisonEvidence.current propagates non-rate-limit transport errors only
  in owner mode. require_comparison's default wait=True follows the old path.
- Both shared publication functions use the original list_prs lookup with the
  flag off; exact-number get_pr and issue-closed handling are owner-only.
- Legacy queue _live is restored to its original running-worker predicate.
  Durable admission liveness is evaluated only by the independent owner.
- record_merge is restored to its original implementation. Post-worker
  completion uses the new owner-only record_owner_merge method.
- The target guard in after_merge, the separate runtime budget, ownership reads,
  recovery and process launching are behind merge_owner.
- Optional tracker arguments retain their original defaults and gh arguments.
  RejectedEvidence is emitted only by the owner adapter. Shared signatures and
  empty queue metadata preserve the old calls and serialized legacy records.

Historical owner_merge data remains a completion fact if the feature is later
disabled. Read-only accounting therefore recognizes that explicit stored record;
legacy histories without it retain their original outcomes and computations.
This is the interpretation of flag-off compatibility used here. The locked
flag-off merge-path contract and the explicit 502/local-fallback regression are
part of the final verification; the whole legacy suite is not being run.

## GitHub read budget

The 40-admission regression uses distinct real Git commit objects, one ordinary
pending workflow per PR, the real GhTracker adapter and an offline gh transport.
One scheduler tick launches one owner pass. It expects **280 gh calls total,
160 CI-related**, with every request unique:

| Fact per pending PR | Calls for 40 |
| --- | ---: |
| PR identity/state/head | 40 |
| Issue approval/body/state | 40 |
| Ordinary check status | 40 |
| Changed files (shared with CI provenance) | 40 |
| Head check runs | 40 |
| Workflow provenance for the one ordinary suite | 40 |
| Parent | 40 |
| Total | 280 |

The pending pass never enters final publication, so it has no fresh locked
recheck. Scheduler GitHub reads use its offline tracker in this measurement;
this is an owner workload count, not a production gate-wide ceiling. More
workflow suites or paginated responses can require more transport calls. The
scheduler's process launch itself makes zero owner GitHub calls.

## Migration, limitations and installation

Drain old workers before enabling; do not convert old ready rows without a
verified handoff. Same-version legacy live turns retain their priority. Drain
owner admissions before disabling or rolling back. Existing checks, approvals,
acceptance hashes, policy and expected-head merge checks remain mandatory.
No live GitHub request, gate deployment, PR merge, commit or push was performed
by this work. Offline Git transport uses fast-import/fetch/update-ref.

The old source retirement estimate remains 445 gross lines: run_merge_turn's
worker wait functions (113), run_publish_flow's post-CI wait/refresh/group tail
(151), run_publish_reviewed's equivalent tail (113), and orch.merge's direct
publication tail (68). These are migration candidates, not deletions here.
Keep the shared verification, final locked reads, journal and priority tools.

Unproven: production GitHub concurrency and latency; migration of running
workers; independent state directories; N>1/group reuse and bisection;
batch-parent publication; full legacy-suite compatibility; and exactly-once
GitHub/hook effects across crashes. Hooks/advice can replay. Candidate refs and
preserved branches still need a retirement policy. The independent process
serializes its merges and does not impose a CI stall escalation policy.

Installation recommendation: **do not install this prototype yet**. Focused
regressions can establish the repaired contracts, but do not establish the live
migration and operational behavior above.

## Verification for this revision

The four requested files ran sequentially, without xdist or a whole-suite run.
All used pytest's default temporary directories: no TMPDIR or --basetemp
was supplied. The prototype and locked merge-path files each ran exactly once,
after both the sandbox and regression files passed.

| File | Final result | Duration |
| --- | --- | --- |
| `tests/test_merge_owner_e2e_sandbox.py` | 1 passed | 49.12s |
| `tests/test_merge_owner_regressions.py` | 11 passed | 55.36s |
| `tests/test_merge_owner_proto.py` | 66 passed | 394.97s |
| `tests/acceptance/test_merge_path_real_settings.py` | 5 passed, 3 xfailed | 133.94s |

Exact commands, in final verification order:

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$PWD/src" /Users/apple/Dev/foragent/.venv/bin/python -m pytest -q -p no:cacheprovider tests/test_merge_owner_e2e_sandbox.py
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$PWD/src" /Users/apple/Dev/foragent/.venv/bin/python -m pytest -q -p no:cacheprovider tests/test_merge_owner_regressions.py
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$PWD/src" /Users/apple/Dev/foragent/.venv/bin/python -m pytest -q -p no:cacheprovider tests/test_merge_owner_proto.py
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$PWD/src" /Users/apple/Dev/foragent/.venv/bin/python -m pytest -q -p no:cacheprovider tests/acceptance/test_merge_path_real_settings.py
```

The first sandbox attempt in this revision failed once in 30.55s after all
four workers had queued and exited: its fake PR-files endpoint compared the
current base directly with the head, so an unrelated new main test looked like
a PR deletion. The final run uses GitHub's merge-base diff semantics. Isolation
now binds HOME, XDG paths, scratch directories and subprocess working directories
to the fresh sandbox, regardless of where pytest allocates it. No behavioral
assertions or expected-red entries were weakened. The sandbox ran twice total;
each other file ran once. Production merge code was unchanged in this revision.

The six defect classes are exercised by the 11 passing regressions. The sandbox
proves four real origin-main commits in priority order, exited worker processes,
one lost-test repair and replacement PR, and restart replay without duplicate
merges or issue closures. The 66-case prototype run also verifies the
280-total/160-CI-call bound for 40 pending admissions, process identity guards,
lock handoff, crash recovery, accounting and flag-off fallback.

The locked file's three strict expected failures remain: pre-admission lost-test
comparison in the legacy path, watch overlap work, and combined CI/group reuse.
These are expected failures, not passing coverage. Production GitHub races,
launcher/signals, migration, N>1 groups, batch-parent publication, hook
exactly-once behavior and full legacy compatibility remain unproven. The
installation recommendation remains **do not install this prototype yet**.
