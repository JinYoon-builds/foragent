# Decisions, planning context, measured overlap and pstack

Order ID: `2026-10-31-decisions-stall-overlap-pstack`.

This delivery is an unpublished order, not its implementation. Four independently
verifiable medium issues use the existing order, view, orch and skills lanes in
`repo/.foragent.yml`. No new lane or fixed lane skill is needed. Each lane chooses
its implementation and any useful on-demand skills. No live issues or running
acceptance files are changed by preparing this order.

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

For older issues without an explicit decision marker, count exact D-number
mentions in their published `docs/specs/*.md` order spec as links. Associate a
spec with its issues using the existing plan-child id, parent issue map, or
published spec reference. An unassociated Markdown file is not a published issue
link; an ordinary issue title/body mention is not this fallback. D74 must not
match D740. Repeated citations and multiple associations do not duplicate links.
Existing plan-child markers and filenames are enough; do not infer similarity.

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

The four issue file sets do not overlap. blocked_by is empty for each: none needs
another's new interface. Decision IDs cited here are semantic references from the
person, not new dependencies. D79's old #232 prerequisite already has an existing
partial-pack mechanism and targeted acceptance tests in this snapshot; do not
add a stale blocker or query GitHub to rediscover it.

Sources for upstream inspection:
- https://github.com/cursor/plugins/tree/main/pstack
- https://raw.githubusercontent.com/cursor/plugins/9b93e81fb2921b9263d9463c15bafeabfe3903aa/pstack/README.md
- https://raw.githubusercontent.com/cursor/plugins/9b93e81fb2921b9263d9463c15bafeabfe3903aa/pstack/LICENSE
