# Decisions, planning context, measured overlap and pstack

Order ID: `2026-10-31-decisions-stall-overlap-pstack`.

This review updates the published order (#1025–#1028), not its implementation.
Keep the four published keys and test names. One additional medium notification
issue uses the view lane because its delivery/configuration files differ.
The five issues use the existing order, view, orch and skills lanes in
`repo/.foragent.yml`. No new lane or fixed lane skill is needed. Each lane chooses
its implementation and any useful on-demand skills. No live issue, main checkout
or running acceptance file is changed by this local review.

## Shared constraints and evidence

D98, supplied by Jin for this order and decided 2026-10-30, governs every issue:
the plan states WHAT; the lane owns HOW. Lock CLI output, persisted files and
rendered text, not private helper names, module structure or an agent's step order.
The entry points and wire formats below are acceptance boundaries. Other
reversible details belong to the implementing lane.

The supplied incident facts are the reason for this work: stale decision labels,
#980's statusline-only stall delivery, five issues waiting behind overlap 3 on
10-30, and D79's approved but unfinished principle import. Code observations in
this snapshot establish the missing mechanisms; they do not independently prove
the live incident counts or the implementation status of D71/D74/D77/D82.

The snapshot has 97 decision headings, D1 through D97; 34 end exactly in
`— 미구현` (the supplied incident description says 98 and 43 respectively).
D98 is absent here but is authoritative in the current request. Preserve the
decisions, their reasons, dates and meaningful title punctuation. No AGENTS.md
or docs/principles.md exists in this supplied tree; apply the principles recorded
in decisions.md, the repository's fa-order instructions and this request.

No meaning classifiers, LLM calls in scripts, invented cost thresholds or
mandatory agent step sequences are authorized. D80/D93 put judgment with the
agent; D95 gives each persisted fact one writer. The gate enforces explicit
decisions and existing safety boundaries. No GitHub call is required by the
acceptance tests. Order preparation never publishes, commits, pushes or edits the
main snapshot. Keep targeted tests serial, with `uptime` below 8 before pytest.

## 1. Decision links and derived state — order / m

**Decisions cited:** D11, D65, D68, D77, D80, D95, D98. D71/D74/D77/D82 and
D79 are examples of why hand labels cannot be used as implementation evidence.

**Why / root cause.** `order.validate_plan` rejects a `decisions` field and the
publisher writes no decision marker. `docs/decisions.md` has manually maintained
implementation labels. Local issue snapshots omit completion reasons even
though tracker.Issue already has state_reason. A closed cancellation therefore
cannot reliably be distinguished from completed work by today's local readers.

**Goal.** Allow optional `decisions: ["D79"]` on issues and edits. Omission keeps
older plans valid. Validate a list of syntactically valid D-number identifiers,
deduplicate it and publish one canonical marker per issue:
`<!-- foragent:decisions ["D79"] -->`. Republishing or editing replaces the
link set rather than appending stale markers. The explicit marker, including an
empty set, is authoritative for that issue. This issue need not add a plan-level
decisions field as well.

On edits and republication, omission preserves an existing explicit marker;
an explicit empty list clears the links and remains authoritative over legacy
fallback. IDs in that field identify ledger headings (`D79`); `D79-1` is a
subclause citation of D79, not a separate decision. Legacy subclause references
link the parent heading without matching unrelated D-number prefixes.

Expose `fa decisions --json --repo-root ROOT` as an offline, read-only JSON list.
Each row has `id`, sorted unique integer `issues`, and `state`. Include every
decision heading. State is `decided, nothing ordered` when there are no linked
issues; `done` only when every linked issue is merged or closed with an observed
completed reason; otherwise `in progress`. Cancelled, reopened, missing or
unknown completion facts never prove done. Treat this as a linkage/completion
view, not a claim that code fulfills every sentence of a decision. Preserve
completion and merge facts through existing snapshot writers; do not add a
second status file or fetch GitHub from this command. Missing/stale observations
must remain distinguishable from observed completion.

A current reopened issue is in progress even if an earlier run/PR merged.
Expose `observed_at` on each row (oldest relevant successful issue observation,
null if unavailable); command execution must not refresh this time or fabricate
freshness. State describes the last observations, not live GitHub or semantic
implementation. A failed refresh retains its last timestamp. Preserve that
provenance through existing snapshot writers. With no readable ledger, distinguish
unavailable input from a successfully read empty ledger in the CLI diagnostic;
the lane decides the presentation. This command never edits a user's ledger.

For older issues without an explicit decision marker, count exact D-number
mentions in their published `docs/specs/*.md` order spec as links. Associate a
spec with its issues using the existing plan-child id, parent issue map, or
published spec reference. An unassociated Markdown file is not a published issue
link; an ordinary issue title/body mention is not this fallback. D74 must not
match D740. Repeated citations and multiple associations do not duplicate links.
Existing plan-child markers and filenames are enough; do not infer similarity.

Legacy prose supplies reference evidence, not implementation intent. Keep the
requested exact-reference fallback and do not classify mentions into principles
versus implementation by keywords or a model in a script. The local measurement
is 35 of 36 headings containing 미구현 cited somewhere in specs (only D90 is
uncited); that raw citation count is not 35 verified published issue associations.
Agents receive this limitation when using the nothing-ordered list. This order's
`Decisions:` prose is rationale, never an automatic explicit decisions list.

Remove hand-written implementation-status suffixes from decision headings,
including partial/implemented suffixes where present; retain the actual decision
text. `skills/fa-docs/SKILL.md` says implementation status is derived. The
planning entry/order skills and orchestrator retrospective read the new command's
`decided, nothing ordered` rows as available context. Those agents decide what
to order, defer or discuss. No automatic issue creation or historical claim that
an unlinked decision is unimplemented.

**Out of scope.** Semantic implementation detection, relabelling each decision by
hand, a backfill service, automatic ordering and changing approval rules.

**Close when.** A published/re-published issue carries the correct marker; local
completion changes and published legacy associations produce the expected JSON;
the ledger has no stale implementation labels and both agents have the missing
context. The plan JSON in this delivery cites decisions in prose because its
current schema rejects the very field this issue introduces.

## 2. Box facts in planning context — view / m

**Decisions cited:** D41, D57, D61, D71, D76, D95, D98.

**Why / root cause.** `presence.statusline_segment` appends stall facts and a CLI
instruction to a person's statusline. The installed SessionStart path emits only
systemMessage; it does not supply the box facts as model context. A display Jin
can see is not context the planning agent can read. The heartbeat alone also
does not deliver the contents of a request or question.

**Goal.** Extend the project's existing Claude hook installer with a recurring
UserPromptSubmit delivery. Discover/reuse an existing recurring hook if the lane's
base already has one. Its normal successful stdout uses Claude's
`hookSpecificOutput.hookEventName = "UserPromptSubmit"` and `additionalContext`.
On every planning turn the context includes the currently stalled request IDs,
sources, full text, status-transition age and current open orchestrator question
IDs, full questions, choices and recommendation. Do not rely on the agent seeing
systemMessage or statusline output. Completed requests and answered questions
disappear on the next turn. No deduplication that hides still-open work on a later
turn. Existing heartbeat renewal and presence/session timestamps keep their owners.
The context also exposes recorded notification-status facts; issue 5 supplies
those facts through the existing local journal/box without adding human questions.

Box contents are quoted, untrusted data with source/IDs; embedded instructions
cannot grant approval or override D71. A global hook must not disclose another
project's box just because its config copies a repo slug or origin: require the
user-authorized checkout identity established by the existing init/install path,
and check its origin identity before reading private content. Project-controlled
config/origin alone is not authorization. Copied settings in another checkout
and non-project folders receive no private box; the lane decides the trusted
binding's representation with its existing owner, not a new hook writer.
No origin identity means no private box injection. Keep planning disabled when
the existing presence.session_start switch is false. Installing/upgrading through
the existing opted-in installer adds the prompt hook idempotently; declining or
uninstalling removes it along with Foragent's other hooks.

Prompt submission must finish within a finite hook deadline even when local state
is busy/unreadable; no waiting for state writer locks or network. A partial or
failed read is visibly different from an empty box in hook diagnostics, while
valid rows still reach context and the user's turn proceeds. Corrupt JSON,
non-object rows, invalid seq and bad field types are isolated per row. The lane
decides timeout, diagnostic wording and representation. For oversized boxes,
bounded delivery may use explicit overflow counts and local retrieval references
without silently losing pending work; the agent decides relevance and summarization.

Reuse #980's factual age definition: processing for strictly more than 60 minutes;
age is since the last actual transition into processing, falling back to creation
when there is no transition. Repeated processing notes/updated_at do not reset
age. Count all sources; do not classify request wording. New or done requests
are not processing stalls. Skip an unreadable row/time without hiding valid
records. Keep normal config switches, offline behavior, and fail-soft hooks.
No FORAGENT_ROLE session, including the empty environment value, and no gate
worktree (including symlinked paths) receives planning-only context. Preserve
other user hooks and remove Foragent's added hooks on uninstall/decline.

The planning statusline has only the additional fact the person can act on:
the number of open orchestrator questions, not the number of stalled requests,
pending issue labels or guessed intentions. Use i18n and plain text, with no CLI
instruction: Korean `기획 세션에 물어볼 게 N개 있어요`, English
`N questions need your answer in the planning session` (singular-safe copy for
one). With zero questions omit that additional segment. Preserve unrelated
presence counts and the existing user's output bytes. Stall facts belong in the
agent's context and continue to be available there even with open questions.

**Out of scope.** Sending answers, automatic retries, new notification services,
interpreting inbox text, changing D71 or the 120-second heartbeat contract.

**Acceptance ownership.** Existing locked test_presence_order_stall.py asserts
the old stall-and-command suffix throughout. Those UI assertions are intentionally
superseded by this order, not implementation regressions to fix by retaining the
bad suffix. This plan contains its complete replacement under the same path, so
the changed acceptance contract can be reviewed and re-approved at publication.
No currently running issue's test is edited during preparation. Keep the old
transition/legacy/bad-time semantics covered in the replacement. Other presence,
session-start, report cursor and uninstall contracts remain in force.

**Close when.** Running the actual installed prompt command twice delivers fresh
box contents each time; closing items removes them; role/worktree exclusions,
age boundaries, and localized question-only statusline behavior pass.

## 3. File overlap selected from records — orch / m

**Decisions cited:** D60, D69, D77, D80, D82, D87, D93, D95, D96, D98.

**Why / root cause.** `overlap_files.at_capacity` reads only the configured scalar.
Per-file overlap counts, conflict counts, resolution_seconds and redo_count already
exist in run history and `fa orch facts`. Their presence does not give the
orchestrator a file-scoped control. The project sets overlap_per_file to 3; its
comment records 1 conflict (598 seconds, no redo) in 107 overlaps, earlier 33
conflicts at 189 seconds median, and five ready issues waiting for hours. These
are reasons to revisit a cap, not evidence for a universal new cap or threshold.

**Rule.** In normal measured mode, enforce the current unexpired agent/human plan
entry for each file. The orchestrator judges whether to raise, keep or lower it
using that file's recorded overlapping executions, conflict rate, resolution
minutes (seconds / 60), redo count, waiting work and available capacity. Low cost
can justify more overlap; expensive conflicts can justify less. There is no
automatic definition of "low", no LLM inside the gate and no classifier. Retain
the evidence/reason, author and time with the chosen limit so a retrospective
can compare the consequence. Reading more records alone never rewrites a plan.

Provide `fa orch plan --overlap PATH=N --why TEXT --by human|orchestrator`,
repeatable with the existing expiry/JSON controls; `PATH=off` clears an entry.
N is positive and counts total concurrent runs sharing a path, including the
candidate. Expose the path, limit, author, reason and expiry in the public plan
and record the change in the existing event journal. Reasons are opaque
agent-supplied evidence, not text the command judges. Keep the existing single
plan writer and the prohibition against the orchestrator replacing a human item.

An overlap edit's expiry applies to the edited file entries, never changes the
life of an unrelated human queue/width/expiry/file entry, and cannot evade their
author protection. Without --expires, preserve an existing file expiry or leave
a new entry without expiry; the public view makes that explicit. This extends
the current whole-plan expiry interface only for overlap edits. --by records
cooperative authorship, not an OS authentication boundary. Paths identify concrete
repository-relative files under the same case policy as file claims: equivalent
in-root spellings identify the same protected item; escaping the root is refused.
Do not turn overlap keys into glob policies. The lane decides canonical storage.

Without an applicable live file entry, use existing overlap_per_file as the
fallback (including today's configured 3); expiration/clear restores that fallback.
Retain a deliberate config override: `overlap_mode: fixed` always enforces
overlap_per_file and ignores measured entries; omitted/`measured` uses the rule
above. This replaces the scalar as the only control without changing its legacy
default or silently rewriting this project's policy in preparation.

Apply the same effective limit to scheduler admission and the runner's existing
late-file boundary; keep case/glob intersection, union-file behavior, exclusive
reservations, unknown approvals, machine budgets and blockers intact. A concrete
entry applies only to the exact file it names; a glob claim that may cover several
files must satisfy every applicable limit and use the fallback for uncovered
paths. Do not replace safe glob matching with string equality. Runtime reductions
do not kill existing workers; they hold new conflicting admissions. The plan
reading skill has these facts, controls and the goal of avoiding idle work while
keeping conflict cost visible. The agent owns its reasoning and workflow.

**Out of scope.** A new conflict collector, numerical "cheap" threshold, automatic
cap tuning, changing merge conflict resolution, architecture auto-escalation or
raising machine budgets to make the test pass.

**Close when.** A record-backed file decision allows five workers where fallback
3 held them, a sixth waits, exclusives still block, a human entry is protected,
expiry restores fallback, and fixed config overrides the file decision.

## 4. Pstack portable principle library — skills / m

**Decisions cited:** D23, D58, D77, D79-1, D79-3, D79-4, D79-5, D95, D98.

**Why / root cause.** Only principle-fix-root-causes and principle-prove-it-works
are present. SkillManager supports local libraries and per-issue installation,
but library mappings accept only name/repo/path; sync uses pull --ff-only and
cannot pin an upstream revision. Catalog CLI also reads global libraries only;
project-local vendored skills are installable by known name but undiscoverable
in that catalog. A generic unpinned registration would expose unrelated Cursor
skills and drift over time.

**Goal.** Reuse fa skills/skill_libraries, not a second catalog. Because this
snapshot lacks pinned-upstream support, vendor the approved portable principle
set with provenance. Make the project's selected library visible to
`fa skills catalog --json` and installable on demand through its returned IDs.
Only metadata is loaded for discovery; do not automatically attach/install all
principles or add them to every lane. Preserve the two existing imports and lane
skill selections. The exact set has 24 principles including those two; the
acceptance test lists the approved names. Keep one authoritative copy of each.
The vendor location, registration details and package layout belong to the lane.
Project-local discovery must not discard existing global libraries.

Use upstream cursor/plugins pstack at commit
`9b93e81fb2921b9263d9463c15bafeabfe3903aa`, already cited by the two imports.
Record repository URL, commit, original paths, Lauren Tan's copyright, full MIT
license and any local portability edits. Do not import Cursor control/setup,
model routing, automations or unrelated procedures. Read each selected principle
for tool-specific references; the lane decides any necessary portability edit
and records why, rather than running a keyword classifier. Principles remain
optional agent guidance and do not override this project's decisions or approval
boundaries. The existing two SKILL.md files stay byte-identical.

The web reader could read the public upstream README and MIT license at this
commit and identify the 24 principle names. Some individual source/tree requests
returned cache misses. Direct terminal fetching failed with DNS error
`nodename nor servname provided, or not known`. Fetch and inspect the actual
selected files in the implementation lane's network-enabled worktree; do not
substitute invented content or treat cached README metadata as verified SKILL.md
bytes. If upstream cannot be fetched there, record the external blocker and keep
the two already imported skills usable. No implementation download is done here.

An external blocker leaves this issue open; it does not satisfy done_when or
authorize invented source bytes. Use the existing external-blocker/handoff path
instead of repeatedly retrying an unavailable fetch. Local vendored libraries
are read-only to `fa skills sync`: syncing must not pull their enclosing checkout
or move the pinned sources. Catalog IDs must install the source they advertised
even with overlapping global/project names; report ambiguity rather than silently
substituting a skill. The lane decides merge precedence and packaging.

**D79-5 recorded comparison.** Use existing `fa orch scorecard --json`, existing
run/skill-use records and an adoption note in the normal research/work log. At
adoption record commit/time, baseline window, sample size, successful/total runs,
elapsed time, repeated failures and post-merge repairs from the existing scorecard
and linked records. Use comparable before/after windows (initially seven days on
each side) and show sample sizes, issue mix and observed skill use; zero/missing
records mean insufficient evidence, never improvement. Record the after window
when it exists, with a retain/change/remove judgment by the orchestrator. Do not
block library delivery for seven days, invent an after score, create a scheduler,
new evaluation engine, target score or causality claim. The existing retrospective
already reads the scorecard. The adoption note is its inspectable evidence.

**Out of scope.** A generic pinning framework, all pstack procedures, changing the
two existing imports, always-on principle injection, a new scorecard system.

**Close when.** The offline project catalog contains exactly the approved 24
principles with MIT metadata; only the selected principle is installed even on a
repeat install; attribution is retained; the original two are unchanged and the
adoption note records a real baseline and a pending comparison boundary.

## 5. Reliable, personal decision notifications — view / m

**Decisions cited:** D36, D41, D71, D76, D80, D95, D98; Jin 2026-10-31.

**Why / root cause.** 255 claude-push failures since 10-28 were reported. The
CLI resolves origin, but the shared notify.send entry still requires config.repo;
direct blocked/stopped/silence senders bypass the inspect decision-only policy.
The CLI also ignores send's False result, reporting success after an outbox failure.
Jin deliberately disabled pushes because of noise. Missing repository context
and a missing shared personal policy, rather than message wording, cause these
failures. No classifier or new notification service is needed.

**Goal.** With config.repo absent, every notify caller resolves the checkout's
git origin (including linked worktrees and calls from subdirectories), reusing
the existing GitHub origin parser. An explicit config.repo stays authoritative.
No usable origin produces an observable failed delivery/CLI nonzero diagnostic,
never success without an outbox record. Preserve notify: none.

Expose personal `notify_filter: decisions|all` in the existing `me` settings,
with omitted value preserving the current all policy. In decisions mode only
currently open D71 questions, merge approvals and order cards may reach pushes.
Use explicit producer-supplied kinds `question`, `merge`, `order`, `blocked`,
`orchestrator-stopped`, `silence`, `summary`, `status`; extend the public
`fa notify --kind KIND` entry. An untyped alert is status in this mode, never
classified from words. The personal policy is authoritative over project values.
All shared senders honor it; question/merge/order producers must supply their
facts and existing open-item/dedup rules remain. Status/digests do not push in
decisions mode even when their text says “approval”. Keep all mode compatible.

Suppressed blocked, stopped and silence facts reach the next planning turn via
the existing local journal/box and #1026's context, with event identity/source/time
and content. They are agent context, not new human questions. Preserve durable
facts while no planning session is open. Do not consume the person's report
cursor or drop status records when notify is off. The hook reader remains owned
by #1026; this issue adds the facts through the existing state owner and tests
their delivery using that interface. It depends on planning-box-context for that
interface. The lane decides storage/format and producer migration details.

Do not turn Jin's pushes on. After merge Jin selects decisions and enables the
channel himself. The opt-in filter preserves existing notification compatibility.
Close only
when origin fallback and the personal filter produce the correct actual outbox
contents through notify's public entry, and a suppressed status fact reaches
planning context without creating a human question. The all/none compatibility
cases and failures stay observable. No real push/network is used in tests.

**Out of scope.** Automatically enabling pushes, guessing urgency from words,
new notification transports, multiuser authentication and status-to-human escalation.

## Acceptance and review

Each issue's acceptance_files contains the complete proposed Python source.
The issue's done_when runs that source plus only related existing targeted tests.
The mutable draft files are review copies; publication uses their exact embedded
bytes. No private function name, new helper module or internal policy algorithm
is an acceptance requirement. Fixtures reuse this repository's offline CLI,
fake tracker, fake process table and Git fast-import fixture. They run no forbidden
Git command, model process, real signal, GitHub request or terminal window.

Reversible choices in this order: per-issue decision links rather than also adding
plan-level links; a local decisions command; recurring UserPromptSubmit context;
agent-owned file limits rather than a cost classifier; fallback 3 and a fixed
mode override; a vendored portable pstack set; seven-day comparison windows.
These choices are the smallest reading that preserves agent judgment and fixes
the observed missing context/tooling.

The four existing keys and all existing test function names are retained.
The new notification issue consumes #1026's planning-context interface; its
blocked_by names that key. Config and notification callers can overlap other
file sets; existing file admission/merge protection handles this, not invented
dependency links. Decision IDs cited here are semantic references from the
person, not new dependencies. D79's old #232 prerequisite already has an existing
partial-pack mechanism and targeted acceptance tests in this snapshot; do not
add a stale blocker or query GitHub to rediscover it.

Sources for upstream inspection:
- https://github.com/cursor/plugins/tree/main/pstack
- https://raw.githubusercontent.com/cursor/plugins/9b93e81fb2921b9263d9463c15bafeabfe3903aa/pstack/README.md
- https://raw.githubusercontent.com/cursor/plugins/9b93e81fb2921b9263d9463c15bafeabfe3903aa/pstack/LICENSE
