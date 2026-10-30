# Shared workload capacity, resilient admission, scheduled reviews and memory

Order id: `2026-10-30-load-tick-architect-park`. This revision applies Jin's
2026-10-30 decisions to the local export. It drafts an order; it does not
implement production changes, publish, approve, or call GitHub.

No AGENTS.md exists in this export or this folder's ancestors. Follow
`docs/decisions.md` D65/D80/D82/D93/D95: scripts supply facts, arithmetic and
safety; agents judge meaning and interventions; state has one owner and journals
append. No keyword classifier, semantic threshold, scripted lesson judgment,
LLM inside a script, or imposed agent investigation sequence is authorized.

## Evidence and limits

The shared eight-core M1 reportedly reached load 194 during planning, outside
verification and a live lane. Capacity counts gate issue/dream runs; planning
turns bypass it, reviewer panels launch N agents against one run, and Stop-hook
checks lack the existing test slot. These are recorded or obvious load paths.
Adopt baseline and browser previews have no supplied load record and are
excluded, as are doctor diagnostics. Interactive orchestrator/lane-talk sessions
can stay open for days and never hold a machine slot, including resumed runs.
External verification cannot be classified from shell text: the verifying agent
lacked shared-capacity context and must follow the explicit targeted/no-xdist/
load instructions here. No numerical load-reduction prediction is supported.

The failure log is a 12:29 UTC follow_children sub-issues timeout after 120 s,
not a bulk-list failure. tick-outcome.json is a later successful 12:49:59 UTC
tick: 19.113 s total, process_snapshot 0.117 s, log_prune 5.018 s. Neither proves
the cause of reported 785/315/102-second timings. Duplicate journal trimming is
clear from code; broader optimizations remain outside scope. #459/#435 review
failures, r28 waiting and the stale dream PRs are supplied observations.

## Issue map and dependencies

Sections match the issues array exactly. All retained test function names stay,
even where historical names describe superseded mechanisms. Dropped adapters'
new files are removed. Existing lanes follow .foragent.yml.

| Section | Key | Lane / size | Boundary |
|---|---|---|---|
| 1 | planning-budget | orch / m | Shared live-workload accounting |
| 2 | planning-launches | order / m | Prereads and perspectives |
| 3 | worker-launches | orch / m | Workers, N reviewers, Stop/Done-when checks |
| 4 | background-launches | orch / m | Noninteractive role workers |
| 5 | tick-admission | orch / m | Fresh start evidence and bounded optional I/O |
| 6 | park-hold | orch / s | Remove approval on park |
| 7 | architect-schedule | orch / m | Shared schedule, controls, decision append |
| 8 | project-review-facts | orch / m | Spend and scope measurements |
| 9 | orchestrator-retrospective | orch / m | Append notes; reuse schedule hook |
| 10 | lane-memory | skills / m | Append/curation owners and migration |
| 11 | gate-reload | view / s | Small reload recovery and status |

Dependencies express APIs/shared files, not agent investigation order. Worker
adapters consume capacity; role adapters follow worker integration; schedule
follows role accounting and resilient ticks; retrospective extends that hook
and journal retention. Facts consumes capacity. Validate every independent pair
for path/glob overlaps including acceptance files. Capacity and then planning
can ship before later dependent improvements. Re-measure normal operation after
rollout; this draft authorizes no stress experiment or live publication.

## 1. Shared live-workload accounting (planning-budget)

Root cause: capacity.machine_runs counts active issue/dream records and pending
starts across sibling stores but not planning/role sessions or N reviewers.
Add one public capacity.hold_workload(store, config, *, tests=False, wait_s=600)
context behind capacity's facade and a small capacity_sessions helper. Acquire
under the existing machine lock; reuse AIMD, machine ceilings, machine_load,
OVERLOAD_LOAD and critical kernel hold. No new thresholds/controller. Include
sibling stores containing only workload records. Scope is the existing per-user
machine_root, not cross-account OS reservations; host load observes other work.

Use ordinary live-process/run accounting. Keep gate prepare/start/clear and its
existing LAUNCH_GRACE unchanged. Add no token protocol, new launch grace,
PID-start-time handoff, supervisor or exclusive borrowing. Existing run identity
prevents charging an already running worker twice, including deployment-era
workers; acquire before registering a new direct run. Synchronous phases are
one workload. N concurrent reviewers consume N total slots; their waiting parent
is no additional agent turn. Bound actual fan-out by available slots, including
serial completion at budget 1. Merger uses one slot after all members finish.

tests=True also holds the existing test slot; nested fa test reuses HELD_ENV.
Checks know they launch tests without parsing command meaning. Waiting holds no
machine/state/role lock. Interactive cap is 600 monotonic seconds per acquisition
(active waiting time, excluding OS sleep); ticks try once with wait_s=0. Timeout
cannot produce successful evidence, an empty passing baseline or a passed check.
Ctrl-C, exceptions and normal exit release contexts/locks. Dead processes stop
counting under the existing liveness rule immediately, even if files remain;
owners release normally, existing retention can remove stale records later.
Readers never mutate records; workload accounting never signals processes.

Print a localized stderr line at wait entry and on holder/reason changes, with
used/budget/free, holder project/workload/issue or role, blocking reason, and
Ctrl-C to cancel/retry later. Timeout repeats holders and actionable retry
advice. New human messages use en/ko i18n catalogues; JSON stays pure JSON.
This is not FIFO fairness. Sequential prereads may each wait; disclose the cap
and current progress. Tests use fake tables/clocks only, no real signal targets.

## 2. Planning launches (planning-launches)

Wrap each order.preread executor turn and each order_perspectives seat with the
shared context. One seat is one slot; unavailable executors reserve nothing.
Preserve read-only prompts/models/ledgers/cache integrity. Failure, interruption
and timeout release capacity and cannot cache success; other seats' questions
survive a failure. Localized progress explains that seven seats share capacity
and can run in waves. The amended locked seven-way barrier explicitly supplies
seven healthy slots. Tight-budget tests prove serialization and gate contention.

## 3. Workers, reviewers and checks (worker-launches)

Gate issue/dream launches already reserve runs; reuse those records. Direct
run_issue/review_only and hidden workers enter shared accounting before work,
without fabricated admission flags or handoff tokens. rerun-review/resolve-pass
are covered by review_only; resolve-fail only queues. N simultaneous reviewers
are N slots, not one plus exclusive borrowing. The waiting coordinator is not
an extra agent. All member results reach one merger. Done-when tests for both
executors and Claude Stop hooks hold existing test slots; parent machine work
counts once. Codex uses its existing post-turn Done-when path, no Claude hook.

Stop hooks identify project/store/issue from the existing approval snapshot.
Configure the hook timeout to cover wait plus execution, or expire its slot-wait
before the enclosing hook deadline. Killed/timed-out checks block completion;
flock slots release on exit. No adopt/doctor launch changes without load evidence.
Existing baseline prebuild remains unknown/deferred on slot denial, never green.

## 4. Role workers (background-launches)

orch_architect.call/run_session starts actual noninteractive role executor work.
Admit/count it through existing role records/liveness and the shared context,
once through completion/failure. Preserve role identity checks already present;
add no birth-time/handoff machinery. Manual and scheduled roles share accounting.
Orchestrator open/refresh/ensure, lane talk, interactive finished-run resumes,
focus/messages/logs never hold a workload slot. Preserve their session seats.

## 5. Resilient tick admission (tick-admission)

Today tick catches rate-limited TrackerError as a skipped tick; other refresh
errors escape _tick_locked. RecordingTracker preserves issues.json and records
refresh_failure before re-raising. A refresh failure skips
 github_wait_after_refresh, run_recovery, batch_reconcile, board sync/promotion,
parent following, github_wait_before_start, apply_budgets, baseline_prebuild,
dream_check, machine_budget, start_ready and view reopening. Post-locked signals
and normal tidy also skip; outer cleanup/spawn finalizers cannot admit work.
Batch/follow_children errors similarly discard later starts. Admission is skipped.

Isolate expected external read/projection errors, retaining error/failed_at and
old listing as_of. Build candidates from stored data, but after failed refresh
immediately before each reservation/start bypass RecordingTracker's cached
get_issue: exactly one bounded gh GET /repos/{repo}/issues/{n}, no retry or extra
listing. Its current body/labels/open state/PR identity must pass normal
eligibility, approval snapshot/body hash, acceptance/prerequisite checks. Use
existing valid approval provenance; missing evidence skips that candidate. Read
failure/timeout skips that issue this tick and continues independent candidates.
Withdrawn approval, closed issue and edited body cannot start. Preserve rate-limit
pauses, outbox, base/config, quota/hours/capacity guards. Successful-refresh paths
keep existing per-issue/GitHub budgets; never synthesize approvals.

Bound follow_children plus batch reconciliation gh work to a **combined 10-second
monotonic deadline per tick**, maximum **5 seconds per subprocess**, including
pagination/retries. Stop optional calls when exhausted; propagate remaining time
to subprocess.run and record deferral. Fallback candidate read is <=5 s, no retry.
Catching after the old 120-second timeout is insufficient; leave no gh background
thread running. This bounds these optional gh steps, not the whole tick/git fetch.
Tests advance fake clocks at timeouts and check individual/cumulative bounds.

Housekeeping trims journals then prune_logs trims again: remove that duplicate
and keep direct prune_logs trimming. Scanning/lock contention could be slow, but
no exact cause for 315/102 s is proven. No speculative cache or safety bypass.
Existing report/status facts expose failures; new human text goes through i18n.

## 6. Park removes approval (park-hold)

Root cause: park removes blocked/needs-human but leaves approved; waiting-person
does not exclude admission. Park also removes approved, optionally adding
waiting-person with --for-person. Preserve why validation, blocker-clock behavior
and one parked event. No hold store or scheduler park predicate. Unpark removes
only waiting-person plus its existing event; it never approves. Re-approval uses
the existing idempotent fa order publish approval path. Labels/status explain
why work waits even after local state loss. Explicit external approval retains
its ordinary authority; no hidden hold overrides it.

Test both approved queued plain/person parks: no next start; unpark alone still
cannot start; a fresh valid publication approval permits one start. Existing
journal-only parks need operator review and explicit re-park before rollout
resumes; the implementing orchestrator inspects/reports those actions. Do not
infer active holds from old events. Already running work uses existing stop.

## 7. Shared schedule and architect reviews (architect-schedule)

Root cause: architect only launches on orchestrator calls; role signals notify
rather than waking it. Held-execution counts miss many rounds in one run. It
lacked a wake instruction/tool connection. Proposals/r28 remain agent judgments.
Add a small orch_role_schedule due/dispatch hook at existing post-start/idle
housekeeping, reused by section 9. Reuse role execution/liveness, quota/hours,
remaining slots and low OS priority where supported. Ready issue/dream work
gets slots first. No daemon/cron/new timer. --no-start forbids ALL automatic
role/wake launches, including finalizer paths.

One top-level boolean `scheduled_roles: true`, default on, controls BOTH automatic
architect and retrospective. Validate/load/serialize via normal settings and
committed tick config; document in docs/gate.md section 9 (settings reference).
False explicitly holds/drains these project roles. Existing machine-readable
project hold/drain and any live orch_plan pause suppress dispatch AND delivery.
This export has no separate project-drain record: when intent is only prose,
the orchestrator must set this switch or plan pause, never classify the prose
or create another hold store. Conservatively any active lane pause defers these
whole-project roles; expiry permits eligibility again. Manual role commands
retain existing explicit-command semantics.

Architect needs new stored merge/run input since last successful covered_until.
An idle execution lane (no active/admissible work) makes it due immediately;
otherwise it is due at 24 elapsed UTC hours since last success. No previous
review plus input is immediately due. Without new input no review runs. Ignore
role calls/completions/heartbeats/signals as input. Session alone appends done;
success covers INPUT START cutoff so arrivals during review remain due. No
second last-review writer or fixed failure-count trigger.

Failed launch/quota/execution stays due but retries no sooner than one elapsed
UTC hour after failure, or an existing executor reset/pause if later. Share
that retry floor with retrospective delivery/open failures and unacknowledged
pending wakes. Keep one pending request, no per-tick new request/window. Capacity
deferral never advances completion. Derive last attempt/error/next retry from
task events. UTC daily/backoff deadlines include sleep; wake still obeys guards
and starts no catch-up burst. This differs from active-time acquisition waits.

Expose localized schedule facts in fa orch facts: last success/input cutoff,
due/reason, suppression, last attempt/error, next retry and pending age. Missing
role or supported opener is visible unavailable/deferred. Headless/Codex-only
projects can see the pending task and record a marked note; add no backend.
Interactive orchestrator lifetime consumes no slots. Unsupported nice is visible
best effort; capacity/holds still apply.

Append a dated docs/decisions.md decision IN THIS issue: Jin's 2026-10-30 decision
supersedes D82 addendum 5-4 (5-④), "no fixed schedule calls": architect reviews
on its own idle/daily schedule across the whole project. Keep the old decision
as history and proposal acceptance with orchestrator/person. Keep every existing
architect skill section/reference; add Project review, changed merges/run input,
cutoff, structure facts and spend versus size/scope drift versus declared Files.
Amend manual-only provenance. Proposals use fa orch request --from architect;
no mandatory investigation order or automated proposal acceptance.

## 8. Spend and scope facts (project-review-facts)

Extend orch_facts.collect runs and lane-grouped areas with spend/scope_drift,
from one frozen read; preserve existing keys and do no writes/network/launches/
signals. Seconds reuse runtime_seconds, rounds reuse usable recorded reviewer
rounds, size is issue size not executor tier. Reuse ETA's machine-wide latest
merge per issue, last 30 per size within 14 days, minimum 3 samples; defaults
s/m/l=30/90/180 minutes, basis_runs=null. Publish as_of/source: this is the current
measured basis, not a historical estimate stored at approval. Known rounds use
median over that same population/minimum; no invented default rounds. Raw values
and ratios use null for unknown/zero denominators, no severity or trigger.

Use execution-captured declared_files/changed_files and existing glob semantics,
deduplicate actual paths. Legacy unknown is null, not compliant empty. Areas
retain per-execution detail, sums and unknown counts/provenance. Examples:
m runtimes 600/900/1200/1800 -> typical 1050, basis 4; rounds 1/2/3/4 -> 2.5;
one sample -> 5400 s and no typical rounds. Human text uses i18n.

## 9. Note-based retrospective (orchestrator-retrospective)

Root cause: write_note overwrites orch-note.json, destroying the skill's daily
completion evidence. Keep latest-note projection AND append exactly one kind=note
journal event per successful fa orch note, preserving now/why/next/where and time
verbatim. Add explicit `fa orch note --retrospective --now TEXT`; event marker
retrospective=true. Unmarked notes never count. Reject blank input before writes.
No separate retro command/event kind/last-retro file/completion store. Derive
last-retro from latest marked journal note; retention keeps latest completion
even when old notes trim. Preserve existing output/JSON and person-found path;
person_found already exists and must not be reimplemented.

Use section 7's SAME hook/switch/hold/pause/backoff. First eligible tick is due
without a completion, then every 24 elapsed UTC hours, even with no new merges.
Keep one existing inbox request per pending period; deliver to live orchestrator
or supported existing opener, retain pending/error/age/retry if unavailable.
Request/open/ack is not completion: only marked note is. Completion resolves
the period; sleep causes no catch-up windows. No lifetime terminal machine slot.

Orchestrator appends chosen lessons to configured MEMORY.md and records the
marked note. Code records facts, never lessons or their meaning. Keep SKILL.md
<=150 lines and reference references/retrospective.md, preserving action notes,
proposals/role signals/notifications and --person-found. New memory prose follows
project language, retaining quoted evidence. Invent no historical missing events.

## 10. Lane memory split and migration (lane-memory)

Jin keeps the split: workers append MEMORY.new.md; dream alone curates MEMORY.md.
Current workers and dream write the same file; merge=union conflicts with curated
removals. Retry changes for two PRs do not fix ownership. Both sources reach
Codex lane_notes, Claude supplemental notice, dream prompt and lane view (three
lines EACH plus full links), even with absent/short curated memory. Codex gets
the SAME append-only instruction text. Remove native lane curated-write authority;
role memories keep their contract. People append lessons to MEMORY.new.md too;
manual curated edits remain base input and must not be silently overwritten.

Migrate this repo's eight agents and lanes.add_lane template, preserving custom
body/skills/models. Other projects migrate on **fa init re-run**, also new init;
not an implicit fa update sweep. Document that in the recovery reference.
One idempotent lane_memory migration called by init_repo replaces managed old
writer text/native lane-write authority, adds managed append instructions, moves
union to MEMORY.new.md, preserves custom content/curated bytes, leaves role agents
alone. Pending files are lazy; a second init changes nothing. Existing init
publication rules stay; acceptance fakes commit/publication boundaries.

Dream captures pending bytes/base, folds captured prefix and publishes curated
plus consumed-buffer changes together. Keep late suffix appends. Failed execution/
publication cannot clear base. Revalidate exact prefix/current base before publish/
rebase; incompatible base/old appends preserve inputs and retry, no similarity
heuristic. Unpublished/unmerged dreams are not completion. No-change curation
can still require a buffer-clear diff. Old open lane PRs must rebase/reconcile
MEMORY.md appends into pending input with agent review before merge; don't union
into curated memory or resurrect consumed prefixes. Retry from published base.

For #898/#901 implementing orchestrator records both head/base/diffs and carries
lessons present only in those PRs into MEMORY.new.md FIRST, with source references
and preservation diff. Then orchestrator ASKS the person to confirm closing both;
only after confirmation closes without merging and re-dreams current base.
No automatic closure/inferred approval. This draft performs no remote action.
Meaning/contradictions/usefulness remain agent judgments; respect project language.

## 11. Small launchd reload recovery (gate-reload)

Root cause: restart bootouts before migrating and bootstraps once. Failure can
leave no loaded job; detached updater filters absent jobs, losing self-recovery.
Prepare/validate candidate before bootout, retain usable old definition. Initial
bootstrap failure permits one immediate candidate retry and one rollback bootstrap,
maximum three, no repeated bootout/long sleep. Confirm loaded state and record
original error/attempts/recovered/loaded in one latest fact. Keep job loaded where
recovery is possible; persistent OS refusal is explicit, never false success.
Old removed executables use existing stable shim. Rollback MUST retain/apply
D97 osascript JXA wrapping, never an unwrapped legacy definition. No new service.

Cover fa update's between-tick restart and detached refresher retry of a recorded
pending absent job; explicit update/install can recover without another tick.
Status/fa gate status JSON expose reload (null until attempted); human i18n en/ko
shows failure/recovery plus `fa gate install --repo-root <root>` or explicit
update retry. Reads never clear facts. Linux retains heartbeat/staleness, no
launchd reload fact or new platform service manager/push-alert system. All
launchctl/process behavior is fake in tests, including D97 old/candidate plists.

## Validation and rollout limits

Embed full acceptance files, retaining all names except dropped adapters' files;
materialized copies match exactly. Break-it new/changed behavior on unchanged
production source; fake process tables only, never persist os.getpid or signal
real processes. Existing offline Git uses fast-import/fake publication; forbid
commit/push/reset/checkout/stash subprocesses. No live agents or launchctl.

Locked amendments preserve names/assertions: seven-seat barrier gets healthy
seven-slot fixture; architect's universal negative premise narrows to --no-start,
keeping direct merge/status negatives. Safety fixture changes use fake tables.
Land amended locked acceptance with its owner and use the approved amended
baseline before verifying schedule. If policy still freezes the superseded
premise, report the approval/baseline constraint rather than weaken other tests.
Before future publication of retained keys, publishing agent checks active runs
and defers body edits until finish or explicit person stop; no live preflight now.

Only new/changed targeted acceptance and <=3 related existing files, serial, no
xdist. Before EVERY pytest run require uptime one-minute load strictly below 8;
otherwise sleep <=60 s/recheck. This machine's validation cap is not production
configuration. HOME/TMPDIR/cache/basetemp stay inside this folder. Validate real
read_plan/validate_plan plus FakeTracker publish(dry_run=True); real CLI dry-run
lists GitHub issues and is prohibited. Help is read-only. Check schema, sections,
dependencies/globs, names, embedded equality, source integrity and all 72 answers.
Current-run evidence belongs in verification; historical evidence is not a test
result for this revision. Production source stays unchanged.
