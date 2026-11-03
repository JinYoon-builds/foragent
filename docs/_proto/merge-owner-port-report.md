# Merge-owner port onto main

Base: `5c82431f` (`pm/merge-owner-port`), prototype: `a053ec2e`
(`pm/merge-owner-proto`), common ancestor: `232cd955`.

The feature remains opt-in (`merge_owner: false` by default). Workers verify and
append durable admissions before exiting. The independent gate-launched process
inherits `merge-owner.lock`; publication takes `merge.lock`, then main's existing
repository-wide `main_write_lock`, before the fresh final read and merge.

The hard rules override the requested final commit. No commit, push, reset,
checkout or stash was executed. The existing `.git` points outside the allowed
working directory, so the real `git merge --no-ff --no-commit
pm/merge-owner-proto` ran with `--git-dir=.port-merge/git`. That local metadata
reads original objects through an alternate; the original repository metadata
and branch remain unchanged. All source changes are uncommitted in this working
tree. `.port-merge/` also contains execution logs and temporary local merge data.

## Conflict resolutions

| File | Main | Prototype | Resolution |
| --- | --- | --- | --- |
| `src/foragent/ci_mode.py` | Typed GitHub availability waits; detailed comparison refusals/flow records; `accepted_sha` and later measured-base acceptance | Strict owner transport, terminal evidence rejection, nonwaiting comparison | Retain main's comparison, observations and availability typing. Add optional `wait=True` and strict reads; only the owner uses nonwaiting evidence. Rejected owner artifacts return a refusal tuple for the current main comparison API. |
| `src/foragent/orch.py` | Callable/ExitStack imports; retry advice/outbox and retry marker ordering; direct publication through the common main lock | `replace`, exact PR/head/target repair and authorized reviewed handoff | Combine imports. Preserve the original flag-off retry side-effect/read order and direct merge. Route only owner retries through branch-preserving close; only owner human-merge requests enqueue reviewed publication. |
| `src/foragent/orch_dream.py` | Dreams become due on order completion, rather than a merge-count threshold | Count validated owner completions and centralize cleanup | Keep main's order-completion trigger in both modes; recognize owner completion facts only when enabled. Keep central branch-preserving close only when enabled. Drop the prototype's old merge-count trigger. |
| `src/foragent/run_publish_flow.py` | Structured withdrawal reasons, neutral main moves preserve the head/CI, measured-base evidence, PR-aware turn waits | Verified enqueue before worker turn wait; owner retains admission after worker exit | Keep all of main's publication machinery. Add the owner-only handoff, carrying main's measured-base/followed evidence. Keep structured withdrawal unchanged when disabled. |
| `src/foragent/run_publish_merge.py` | Current acceptance mismatch checks; final common main-write lock | Approval-generation binding and exact-number PR reads | Keep both imports and main's lock/acceptance checks. Extra generation/state reads are owner-only; optional authorization preserves ordinary policy defaults. |
| `src/foragent/run_publish_reviewed.py` | Checking phase, structured withdrawals, PR-aware turn waits, current refresh/publication machinery | Refresh journaling callbacks, authorization, exact PR reads and durable handoff | Retain main's phase and turn API. Optional callbacks default to absent. Enqueue/auth/exact reads and retained admissions are owner-only. |
| `src/foragent/run_waits.py` | Resume only after successful acquisition and when requested; current slot accounting | Separate owner advancement runtime budget; unconditional old `wait.close()` | Keep main's complete wait lifecycle. Add only the guarded owner runtime calculation. |
| `src/foragent/runner.py` | GitHub outage detail, remote-ended cleanup guard, current admission/cleanup | Protect durable admissions from worker error cleanup | Combine predicates. Preserve outage details and remote-ended guard; owner-only admission checks prevent worker cleanup from revoking an accepted handoff. |
| `src/foragent/tracker.py` | `retitle_pr` protocol and current outbox/transport implementation | Optional branch preservation, exact PR resource, typed rejected evidence | Preserve retitling and all current tracker behavior. Add owner APIs and optional arguments with identical legacy defaults/argv. Local merge projection waits for validated owner completion only when enabled. |

## Additional integration fixes

- The prototype owner took only the state merge lock. Its final publication now
  also takes the common Git main-write lock introduced by #1662, in the same lock
  order as main's other mergers. Real OS-lock contention tests cover arrival after
  final publication validation and at the merge API.
- Owner bootstrap and admission retries use current main's `TrackerError.waits`
  instead of the prototype's rate-limit-only test. Typed connection drops now
  persist the same shared pause without requesting code repair.
- Flag-off comparison reads keep the original two-argument `_trusted_sha`
  call shape. Passing the prototype's new `strict=False` keyword unconditionally
  broke main's injected read adapters; strict arguments are now supplied only
  for owner reads, without changing existing tests.
- The prototype added state/head fields to the shared `PullRequest` dataclass,
  changing flag-off serialization. They now live in `OwnerPullRequest`, returned
  only by the new exact-number API. Existing listings and FakeTracker creation
  retain their original record shape; the original tracker assertion is unchanged.
- Existing reinstall and orphan-recovery entry points accept lightweight
  contexts without the new feature setting. Absence now means the documented
  default, false, so the port does not require new fields from legacy callers.
- The prototype's fallback command discovery is restricted to owner mode. The
  original local fallback expression remains the flag-off path.
- Persisted owner approvals and comparison evidence now hydrate through main's
  `records.load_record`. The prototype's direct dataclass keyword expansion
  rejected fields written by a newer process. The new exact-PR fake adapter
  uses the same loader. Runtime regressions cover future admission fields on
  both an unchanged base and a refreshed base; Config remains strict.
- Current main removed `pr_acceptance_checks` and the provenance reads from
  ordinary `pr_checks`. The unused snapshot API name was dropped. The same
  40-pending-admission regression now pins **200 total / 80 CI-related** unique
  reads, instead of the historical 280/160, and checks for no deferred errors.
  The scorecard fixture supplies the current API's required issue snapshot.
- The sandbox GitHub service now supports PR issue resources at `/issues/PR`,
  which current main's recording tracker reads when recording PR comments. Its
  simulated clock advances to a persisted GitHub resume time before the next
  pass, including replay after a lost successful merge response.

No new agent judgment, classifier, keyword list, similarity threshold or forced
agent step order was introduced. The changes concern state, identity, locks,
transport and test fixture fidelity.

## Removed prototype paths / S2

No prototype-modified source file was deleted in the supplied main HEAD:
`git diff --name-status --diff-filter=D 232cd955 HEAD -- src/foragent` is empty.
No source file was resurrected, and no prototype file therefore needed dropping.
The supplied HEAD includes #1638, #1639 and #1640; #1641 is not in its ancestry.
The existing main batch compatibility tails were retained; batch review remains
unavailable through validated configuration. The removed old dream merge-count
trigger was not restored.

## Status of the six prototype finding classes

The prototype describes six classes, rather than six separately numbered open
findings: lock ownership, resource identity, durable transitions, transport typing,
read reuse and accounting. None becomes wholly obsolete just by rebasing.

| Finding class | What current main already supplies | What still belongs to this owner port |
| --- | --- | --- |
| Lock ownership | #1662 supplies the common lock for every main writer | Attach the new owner to that same lock; retain independent owner/tick lock separation |
| Resource identity | #1677 keeps re-review scoped to the PR's own changes; #1688 can carry verification over a neutral base move without changing its head | Exact PR number, admitted head/target, generation identity and branch-preserving repair remain necessary |
| Durable transitions | #1679 keeps approvals usable across holds and retries | Admission, refresh/repair intent and merge-response replay remain owner-specific |
| Transport typing / availability | #1644 already supplies typed outages, shared GitHub pause and resumable waits | Reuse it in the owner; keep missing/stale evidence distinct from rejected artifacts |
| Read reuse / scheduling | #1717 prevents collection from waiting for lane test slots; ordinary check status no longer performs the old provenance queries (80 fewer reads for 40 pending PRs) | Owner snapshots, mutation-boundary freshness and the remaining 200-read / 80-CI-read bound remain relevant |
| Accounting | Main already records ordinary worker merges | Exited workers need validated generation-bound owner merge facts and compatible readers |

Thus a separate new main-write lock, separate GitHub-outage mechanism, or a new
collect-versus-lane-slot workaround would duplicate fixes already on main. This
port uses their existing implementations.

## Verification

All requested files ran sequentially: 43 unit files and 188 acceptance files,
with the feature off in the existing tests. There was no xdist or whole-suite
invocation. Every top-level pytest command used `nice -n 15`; the execution sandbox rejected
`setpriority`, so lowered OS priority cannot be claimed. All temporary roots and
HOME/XDG fixtures were inside this working directory. Bytecode was disabled;
top-level pytest commands disabled the cache provider.

| Verification | Result |
| --- | --- |
| Final prototype sandbox | 1 passed |
| Final prototype regressions, including OS-lock races, typed outages and future state fields | 17 passed |
| Final prototype cases | 66 passed |
| Existing requested test files, using the final reruns for repaired files | 3,180 passed; 210 failed; 3 xfailed |
| Five repaired compatibility files, rerun in full | 555 passed |
| Original `5c82431f` source comparison | All 210 remaining failed-case identities match, across 26 files |

The first existing-test sweep had 3,162 passed, 228 failed and 3 xfailed.
Eighteen failures were port incompatibilities; all are resolved by the five
whole-file reruns. The remaining 210 failures are **not passing coverage** and
were not hidden with new xfails or edited assertions. They reproduce on the
unmodified starting HEAD under the same temporary-directory constraint.

The initial prototype attempts also had four failed test outcomes across three commands: one sandbox
fixture failure, two failures in the full 66-case run, and one remaining scorecard
fixture failure on the focused retry. Their causes and all subsequent commands
are retained in the [complete command/result report](merge-owner-port-tests.md).
The final three prototype files above each passed in full on the final source.
Raw per-file logs, command metadata and the exact failed-case comparison are in
`.port-merge/results/`.

The six specifically requested main behaviors have these direct checks:

| Main behavior | Existing acceptance result |
| --- | --- |
| #1662 common main-write serialization | `test_main_write_serialization.py`: 10 passed; owner contention also passes in the final regression file |
| #1688 neutral main moves preserve the head | `test_neutral_move_order.py`: 13 passed; `test_base_move_reuse.py`: 14 passed |
| #1677 review only the PR's own diff | `test_review_pr_changes_after_pass.py`: 10 passed |
| #1679 approvals survive holds | `test_approval_record_retry.py`: 4 passed; provenance unit file: 9 passed |
| #1644 GitHub drop waits | `test_github_drop.py`: 16 passed |
| #1717 collect does not wait for lane slots | `test_publish_collect_check.py`: 17 passed, 6 baseline-identical importer-refusal failures; its slot case returns and fails at refusal, not a slot wait |

No existing test function was deleted or renamed. `tests/acceptance/` has no
working-tree changes. No commit, push, reset, checkout or stash was performed by the port commands.
No production installation or deployment was performed; installations in test
fixtures used their isolated directories. Original branch HEAD remains
`5c82431f`; the separate local merge metadata preserves the resolved merge.

### Remaining baseline failures

Each row below is reproduced with original HEAD source, with the exact same
failing test identities. These are the observed failing boundaries; this port
does not claim to repair these unrelated contracts.

| File | Failed cases | Baseline cause / observed mismatch |
| --- | ---: | --- |
| `tests/test_orch_scorecard_unit.py` | 1 | The workdir-only basetemp is inside a Git checkout; the test assumes its plain folder has no repository ancestor. |
| `tests/acceptance/test_auth_failure_retries.py` | 8 | Current main does not perform the authentication retry/resume behavior this contract expects; transient and bounded retry assertions fail. |
| `tests/acceptance/test_baseline_prebuild.py` | 2 | The consumer does not emit the prebuild-wait state observed by the fixture before its timeout; the same two wait assertions fail on original HEAD. |
| `tests/acceptance/test_ci_base_cache.py` | 31 | The producer leaves FA_BASE_CACHE_FILE absent and reports a head without the CI-mode exclusions trailer; every cache contract stops at that boundary. |
| `tests/acceptance/test_fa_lane_relabel.py` | 6 | The current tick does not perform the lane relabel/config alignment expected by these tests. |
| `tests/acceptance/test_github_outbox.py` | 5 | The current outbox keeps multiline transition comments and, for one case, an additional remove-label entry; the locked contract expects one-line/different queue contents. |
| `tests/acceptance/test_merge_group.py` | 13 | The expected r17 group claims and merge_group_size facts are absent; ordinary member merges still occur in several scenarios. |
| `tests/acceptance/test_one_entrance_lesson_context.py` | 1 | The implementation prompt lacks the other lane path map required by this contract. |
| `tests/acceptance/test_one_entrance_lessons.py` | 1 | The requested fa lanes lesson append tool is not a current CLI action. |
| `tests/acceptance/test_one_entrance_stop.py` | 2 | The bulk orch stop form lacks the required issue number, and fa stop is not a current CLI action. |
| `tests/acceptance/test_presence_statusline.py` | 1 | The workdir-only temporary folder is still below the project configuration, so the outside-project fixture discovers this project. |
| `tests/acceptance/test_publish_collect_check.py` | 6 | Six importer cases expect a refusal when a shared file breaks an importer; current main returns success. The lane-slot case also fails at this refusal assertion, not by waiting for slots. |
| `tests/acceptance/test_related_unit_baseline.py` | 2 | The existing related-test path treats the already-red base test as blocking rather than providing the expected differential baseline result. |
| `tests/acceptance/test_review_budget_batches.py` | 2 | The expected perspective count and concurrent reviewer use differ from the current reviewer pipeline. |
| `tests/acceptance/test_review_grounded_fail.py` | 46 | The separate finding-scoring request/grounding procedure and reviewer-skill contract are not wired as this acceptance contract expects; FAIL is still handled by the current review flow. |
| `tests/acceptance/test_review_perspective_fanout.py` | 1 | The fixture expects seven perspective/merge calls; current main produces six for this scenario. |
| `tests/acceptance/test_review_perspectives.py` | 2 | The current default perspective names/order differ from the locked list. |
| `tests/acceptance/test_risk_map.py` | 15 | Most failures expect six default perspectives plus a merge; current default perspective selection/call counts differ. |
| `tests/acceptance/test_silence_watchdog.py` | 32 | The requested shared foragent.watchdog module is absent on original HEAD. |
| `tests/acceptance/test_ssot_comments.py` | 2 | Current retry/blocked transitions produce multiple or multiline evidence comments; the contract expects one line. |
| `tests/acceptance/test_team_v1_dream.py` | 2 | The expected dream-owner/foreign-memory ownership gates are absent in the current dream decision. |
| `tests/acceptance/test_team_v1_merge.py` | 7 | The expected_base_sha proof API/forwarding is absent; current main guards the head and uses the local main-write lock. |
| `tests/acceptance/test_team_v1_scope.py` | 12 | The expected by-person factory/worker/mutation scope restrictions are not enforced by current main at these boundaries. |
| `tests/acceptance/test_test_slots.py` | 5 | Five fixture assertions do not observe the expected checking/wait state before their watchdog; the same cases fail on original HEAD. |
| `tests/acceptance/test_wheel_scroll.py` | 4 | Enter does not open a popup carrying the fixture Goal marker, before the scroll-preservation assertions can succeed. |
| `tests/acceptance/test_worker_launch_budget.py` | 1 | The perspective reviewer count differs (five instead of six); the two-session concurrency observation itself succeeds. |

The two outside-repository/project cases are consequences of keeping every
temporary file inside the supplied workdir. The reviewer-grounding and lesson
contracts also identify missing agent inputs/interfaces: a grounding procedure
and evidence/decision interface, a cross-lane path map and a lesson append tool.
No keyword classifier, scripted meaning rule, score threshold or LLM scoring loop
was added to manufacture a passing result. These remain original-main findings.

## COVERAGE

Read coverage and test execution coverage are distinct. The coverage inventory
records source files/hunks actually inspected and files not read; executing a test
or parsing its import declarations does not count as reading its full source.
See [the complete read/not-read inventory](merge-owner-port-coverage.md) and
[the exact test commands and results](merge-owner-port-tests.md).
