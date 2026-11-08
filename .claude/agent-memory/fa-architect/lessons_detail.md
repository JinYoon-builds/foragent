---
name: lessons-detail
description: Full text of long architect lessons moved out of MEMORY.md to keep the index under its size limit
metadata:
  type: feedback
---

- `hot_overlap_files` counts co-presence (overlap_records of 3+ runs in 24h), not waits. Before acting on it, sum `kind=ready` `file-overlap` minutes in flow-records.jsonl and the scorecard in-run overlap wait (11-03: 11 min and 0.0 vs slots-full 1358). Then replay `admits` with one peer dropped: usually one late-measured claim (#1727, a 1-line edit) clears at its next merge. (r372) If that peer sits in merge-lock-wait for hours (#1727: 2 h, 11-03), the cost is the merge queue, not the file; check for an in-flight queue issue (#1760) before proposing.

- Unit tests import helpers from locked acceptance modules (20 unit files -> 25 modules; conftest puts ACCEPTANCE on sys.path), so an order commit that edits a locked helper can turn main red with no code change (afd1b174, 11-03: 42 min red, 3 holds). For a red start on an order commit, scan `from test_X import` in tests/*.py against its modified acceptance files. Several PASS rounds on one body_sha usually mean CI red -> rework -> re-review, not a reviewer issue. (r366)

- idle_lanes_with_backlog can fire when idle lanes have 0 ready issues and the whole backlog sits in one lane behind one run's `exclusive:` header (11-03: orch 3 held by #1641). Check `ready_by_lane` and the `kind=overlap` peers first. A 'slots-full 9/9' ready row is one moment (machine budget had just changed); replay `capacity.machine_holders` with the tool-versions python (`tool-versions/current/tools/foragent/bin/python3 -I`) before you believe it. (r460)

- After a feature switch flips (merge_owner, 11-04), check which path the locked tests run: config defaults stay False, so all 12 merge-path locked files (incl. the slowest, test_merge_path_real_settings 196 s) pinned the now-dead path and 0 set the switch. `grep -rln <flag> tests/acceptance` plus the config default answers it. Also grep readers of the old outcome (`outcome == "merged"`): 3 of 11 missed owner merges. (r430, r431)

- The 5A panel (one reviewer per perspective) runs only when `first_review_findings` is empty and risk notes exist (run_review._review), so it ran on 1 of the last 30 FAILs (#1641 r2). Later rounds are single reviewers with #1170's 'First-round defect classes' prompt, which makes 'Remaining X class' chains (#1652 9, #1679 5). Check which review path produced the FAILs (review-R-P.txt files) before blaming the panel. (11-03)

- A post-PASS FAIL with the same body_sha is not proof the PASS boundary failed: `review_pass_scope.for_review` drops the boundary for the whole review when *any* changed path matches a `pass_scope_exempt` risk entry (#1004 r22: 17 label-guard lines in scheduler_recovery.py). Diff PASS head (`review-pass-head` event) vs PR head for the cited file, then check the exempt match, before blaming the reviewer. (r323)

## PR CI load attribution (r547, 11-04)

To attribute PR CI load, list each PR head sha since a cutoff (gh run list) with its parent count and commit title. 'Refresh PR #N onto' and 'Merge commit' heads are base refreshes; check for 'superseded before its CI ended' (next head created before the run's updatedAt). On 11-04, 9 of 47 heads replaced a head whose CI was still running, all made by the merge owner on the PR next in line, and the workflows had no `concurrency:` block. Use `gh run view` sequentially only for about 85 runs within the 2-min limit. (r547)


## plan_pause idle lanes (r554, 11-04)
idle_lanes_with_backlog during a `plan_pause` (facts 'Binding: plan_pause') is the intended hold; read the orch-events `role-signal-answer` for the same r-id first, since the orchestrator often answers not-now before the architect reads it (r554, 11-04: 9 lanes paused for r547, the one startable orch issue #1925 was on the overlap cap 3 for run_publish_flow.py).

## hot_overlap_files with wait 0 (r426, 11-03)
For hot_overlap_files with in-run overlap wait 0, the cost is conflicts on long runs. List conflicted overlap_records for *all* outcomes in runs.jsonl, because the facts 'Overlap conflicts (merged)' line hides unmerged runs. One wide refactor that runs for hours (#1641: 3 conflicts, 88 min, a PASS lost) is the usual peer. The fix is that issue's `exclusive:` header, not a split or a cap change. (r426, 11-03)

## files_over_limit by date (r389)
For a files_over_limit alarm, list the big file's conflicts by date from runs.jsonl `overlap_records` (conflicted, resolution_seconds), not only the 7-day total. order.py's 5/22 all fell on 11-01, during one order-lane batch. After that, 15 commits had 0 conflicts because new work went to new order_*.py modules. Executors split on their own, so a stale total is not a split case. (r389)


## Plan pause released by a blocked green fix (r584, 11-04)
idle_lanes_with_backlog during a plan pause whose release is 'fix X merged': check whether X is green but `blocked`. #1924 (r584, 11-04) timed out on the 1200 s CI wait after a refresh push. The jobs queued 26 min and ran at most 15.5 min. The 08:21 resolve quoted the superseded head (f2eb03 vs PR head bd82a6). Compare the resolve sha with `gh pr view --json headRefOid`, and use job created/started times to split queue from run.

## CI did not finish storms (r599, 11-04)
'CI did not finish' storms: GitHub caps this account at 20 concurrent jobs and a PR head needs 9 (collect + 4 test + 4 acceptance), while `_CIWait.deadline` (1200 s) counts queue time. A FIFO replay with median job times puts 8+ heads at once over 20 min. Synchronized retries then make the next wave. Get per-job times from `gh api runs/<id>/jobs`. (r599, 11-04)

## Outbox-wait after approval batch (r544, 11-04)
idle_lanes_with_backlog right after an approval batch is usually `outbox-wait`: the approvals' own `ready` label writes sit in issues.json `outbox`, so `_start_ready` holds every start for one tick. 11-04 04:53 (r544): 5 orch issues waited, all started at 04:56 with the outbox at 0. Check `kind=ready` reasons and the outbox's `queued_at` before digging.

## Merge batches vs the CI job cap (r615, 11-04)
Read flow-records `kind=merge-batch` (members, result, used_members, ci_minutes, reason) before blaming lanes for a CI-timeout storm. On 11-04, size 6 batches: 2 CI'd batches used 1 member each (180/199 CI min), 3 of 5 died at construction because one `git merge-tree` conflict drops the whole chain in merge_owner_batch.prepare. A PR head here runs 3 workflows = 11 jobs, so a 6-batch is 66 jobs vs the 20-job cap; merges fell 2.4/h -> 0.75/h. Also compare main merge rate before/after a switch with `gh pr list --state merged`.

## Push 500s vs push rate (r618, 11-04)
To count pushes that landed, list Actions runs in the window (`actions/runs?created=A..B`, distinct head_branch+head_sha; the repo events API returned 0 PushEvents in 300 events). Read rejected pushes from runs.jsonl `detail` ('remote: Time ...Z'). On 11-04, 7 pushes were rejected with 500 at <=5 tries/min, while 9 landed with 0 rejected at 08:50-08:55, so rate was not the cause. git push raises GitError and skips #1644's TrackerError.transport wait, so each 500 ended as 'error' and needed an orchestrator retry.


## Stuck merge-owner (11-04)
A stuck merge-owner state is often one long tick, not a missing path: check `ps` for the `foragent.merge_owner` pid's age and its pytest child, and the `merge-owner-*` temp dir mtime. On 11-04 one tick ran a 28-min acceptance baseline inline, so the batch deadline close (prepare, 3600 s from prefix commit time) waited for the next tick. Check github-pause.json for ticks that ended early. (r641)


## Moved from index (11-04)
- For a 'replace ledger X with the git/GitHub fact' census, measure two things per ledger: which field holds the bytes (merge-owner/N.json was 99% comparison.reports, not head/base) and whether the copy disagrees with the GitHub cache (merged_prs.json vs owner facts: 0 of 23 on 11-04). With 0 disagreements, the cost is readers using the wrong copy, so fold the fix into the order that deletes the writer (B2). (r501)
- `runs/issue-N/review-R.txt` keeps only the reviewer's output, not the prompt, so you cannot see from the run dir whether a post-PASS review got the PASS boundary. Check what you can instead: `git diff <review-pass-head> HEAD -- src/` in the issue worktree and the risk-map exempt paths. #1230 (11-03) FAILed twice after PASS on unchanged src, right after a lost-CI-tests retry. (r446)
- 'machine 4/4' with low load is not always AIMD: `fa settings` machine max_parallel outranks machine.yml (machine_budget source `settings`), and capacity.json changed_at moves without decreased_at. Read orch-note.json for a running experiment first. When an experiment cuts runs right after a merge, re-state that merge's absolute per-24h targets as ratios (r313, 11-02).

## idle_lanes after a hand retry burst (r644, r662, r674, 11-04)
- roles idle in lane_concentration on 11-04 were 5 PASSed PRs waiting at merge, not scope (r644).
- idle_lanes_with_backlog seconds after a mass `kind=retry` burst is the orchestrator's hand retry. The tick that is already running misses them, and the next tick (5 min sleep + 90-210 s tick) starts them: ~8 min lag (r674: 4 view/order retries 14:34, started 14:42). Look for the merge stall that caused the retries: `merge_owner_batch.prepare` returning False ends the owner tick before the single path (r662).
- Do not trust `fa orch facts` 'held: structure' for retried issues. The facts replay (_admission) claims a simulated start's *declared* Files (messages/**), but the gate measures a retried run's existing commits (messages/decisions.py). So facts showed false holds that the gate did not apply (r674). Compare `overlap_files.claims(ctx, measured=False)` after the starts, or patch `overlap_files.admits` in the replay to print `admission_holds`.

## Single-path starvation under continuous trains (r732, 11-04)
- `merge_owner._tick`: `if batch is False: return` (train waiting on CI) and `if batch: land; return`. Singles advance only when `prepare` returns None (fewer than 2 composable members).
- Members excluded from batches ('acceptance workflow differs from target' after a workflow change on main, or merge-tree exit 1) can only be repaired on the single path. r662/#1991 assumed that path gets a turn.
- To check: derive each train's start as end minus `ci_minutes` from `kind=merge-batch` flow-records. Count 'merge owner advanced' lines in merge-owner.log, and list phase=waiting merge-owner/*.json files by `ready_at` age. On 11-04, trains covered ~85% of 13:47-21:00Z, there were 2 single advances, and 14 of 23 jobs waited over 6 h.
- Squash merges titled `(#N)` on main by JinYoon-builds from `pm/*` branches are PM hand merges, not single-path merges.

## Moved from the index (11-04)
- A Goal's 'reviewers hold only on X' sentence loses to Done-when checkbox lines the reviewer can quote. Sort each finding into standard / checkbox line / neither before editing again (#1004 round 16: 3/3/1); if checkbox lines supply the findings, move them out rather than add a clause.
- When several rounds' review NOTEs name one sentinel value (None/unchanged) that means several states, grep main for the field first. If the run itself added it (#1641 fallback_reports, r2-r4), the cause is the run's own method. Hint 'do not store the decision; re-read it in each comparison; unknown means read again' instead of one more fix per state. (r455)
- GitHub ignores `.gitattributes merge=union` (lane MEMORY.new.md), so a PR that local `git merge-tree` calls clean can be CONFLICTING on GitHub: no merge ref, 0 checks, then held 'missing acceptance shards: observed []' after 1200 s (#1491 11-04, r704). Reproduce in a `git clone --bare --shared` (reads no in-tree attributes). Do not treat union paths as conflict-free for GitHub. repeated_design_holds counts every 'held', including CI-evidence holds; split held rows by detail first (r705).
  Since #2035 (merged 11-05 00:18Z) such PRs are refreshed; a later 'observed []' hold matters only if the run started after that merge (#1854 01:00Z started 22:42Z, so old code).

## Unit CI shard slow: find the one file (r743, 11-05)
ci.yml unit shards use sorted round-robin by file (ci_unit_shard.select), no duration table; only acceptance shards use scripts/shard_times.json. To find the cause: `gh run download <run> -p 'unit-*'` and sum testcase time per `file` attribute. On 11-04 test_merge_owner_batch.py was 10.8 of 17.2 min in shard 3 (74 queue_workers calls, each a real runner.run_issue x6). The heavy shard index moves as files are added, so 'shard 3' is not a stable name. The locked test_ci_unit_shards pins a file partition, so a balanced select cannot go below the biggest file; split or speed up that file instead.

## Reproduce a conflicted overlap_record before blaming the file (r750, 11-05)
A `conflicted: true` record names the file the run saw, not where the peers collide now. Run
`git merge-tree --write-tree origin/foragent/issue-A origin/foragent/issue-B` on the live peer branches.
For merge_owner.py (r748) the src file auto-merged; the real conflict was both peers appending tests
after the same line of tests/test_merge_owner_regressions.py (341 lines). Also map each peer's hunks to
functions (`git diff -U0 main...branch | grep ^@@`): two peers in one function (_advance) mean no split helps.

## Slow test file: measure inside before splitting (PM r771, 11-05)
r743 found the slow unit CI shard was one file (tests/test_merge_owner_batch.py, 10.8 min, by summing
JUnit artifact time per file) and proposed a split by concern. That treated the symptom. Inside the
file, ~80% of each test rebuilt the same starting queue with ~2,200 real git calls. The cost also hit
every lane run touching the file (#2103 waited ~70 of 84 implementing minutes on tests). Before
proposing a split, run `pytest <file> --durations=0` (setup vs call rows) or a cProfile of one test,
and look for a repeated fixture/start state. Fix owner: PM order 'batch-tests-shared-start'.

## Exempt-match check for post-PASS FAILs (r741)
When a hot_overlap/conflict case ends in post-PASS FAILs, test the exempt match directly: `risks.matching(risks.load_at(git, base), git diff --name-only base..head)` with the fa python (~/.local/share/uv/tools/foragent/bin/python). #1514 (r741): a 2-line flag rename in scheduler_recovery.py reopened full scope, and the reviewer blocked on main's new merge-owner code.

## Detect broadly, accept exactly (r344)
When a scope hint makes the reviewer accept the label NOTE, the executor often widens *detection* and *acceptance* together (#1652 r9: fuzzy `_DECLARATION` regex then accepted `License: MIT` as the short form, a regression vs main). Hint 'detect broadly, accept exactly': fuzzy match only decides what to inspect; acceptance is byte-exact plus an allowlist. (r344)

## Moved from index (11-05)

- Before lowering a Goal bar again, check whether the repeat findings already fall inside the stated bar (#1619 r4-r5: license-with-appended-ban and symlink root both named as holds). If they do, the cause is the method (prefix matching), so give a fail-closed definition as a retry hint (exact template match, realpath equality) instead of editing the Goal.
- Before redesigning a held issue, read its locked test for an allowed minimal branch (`if code:` refusal in test_night_shared_locks) and check whether the reviewers' race scenario appears in any locked case (test_team_v1_scope has none mid-operation). If neither, the fix is to lower the Goal to that bar and name the race a non-goal. (#1004, #1107)
- When a Goal demands that 'every exit' of a run preserve work and each round finds another path (#1491: 3,2,2, then 9 after an 'every path' redesign), find the single irreversible point instead: all worktree discards go through git.remove_worktree (--force). Guard that one call and name the reversible paths (extra turn, push, PR) non-goals. (r377)
- Before accepting a review finding as above-bar or not, check two facts: does a config turn that path off (#1526 group path under `merge_group_max: 1`, 0 group rows in merge-queue.jsonl), and does the finding's input appear in real issue bodies (#1570 fence and marker cases: 0 of the last 400)? Counting the construct beats arguing about the race.
- To measure CI flips after base moves, use gh run list per day (--limit caps at 1000) plus a bare blob:none clone with `+refs/pull/*/head` fetched; a refresh head is a 2-parent commit whose first parent is the passing head. Before calling a flip a collision, check main's own push CI at the merged main sha: most flips were main already red.
- A run that ends 'crashed' ('Crash recovery for running label generation N') within a minute of a `kind=retry` event is the gate's intended restart after a Goal edit (#1491 r377, 11-03), not a crash. Only 2 of 21 such outcomes since 10-27 followed a retry; most come from batch restarts. Match against retry events before you count crashes.

## Command-text handling (moved from index)

A merge gate that re-runs the user's test command on base to get evidence grows one command-rewriting rule per review round (#1105: shlex, -x, -c, conftest, PYTHONPATH). Reuse the run baseline's fixed invocation and its cached passing set (`node_ids`) instead, and keep the user's command verbatim and on head only. Same class recurred in #2103's skip path (r782): skip by collected node ids, never by parsing command text.

Sort a command-handling hold's findings by effect: missed skip (time only), wrong red (passing -> fail), unsafe skip. #2103 r1-r5: 9/5/0; a method hint (r782) did not stop missed-skip findings. Bar: hold on wrong red and unsafe skip only; close wrong red by pytest exit code 2-5 -> rerun verbatim (exit 1 stands, locked once-run tests). (r797)

The merged #2103 wrapper (ci_local.skipping) avoids parsing: --deselect via a PYTEST_ADDOPTS @file, rerun verbatim on exit 5 (r803).

## Function split (moved from index)

When a design splits a function 'after step X' to reuse its tail, read what the head computes that the tail uses (order.publish 581-685: mapping, owner, selection refusals). A 2-way split makes the new caller copy the head, or run the head's refusals after irreversible-ish effects (push). Split 3 ways: prepare / effects / tail.


## Bar/hint timing and admission bar (r803, r810)
- Before measuring a bar or hint proposal, compare the issue's merge time with the retry that would carry it: #2103 merged by PM shortcut 7 s before the r797 bar retry, so r797 is n/a, not failed (r803).- A Goal 'X = exactly what the gate's admission decides' fails on read-only paths: scheduler_start._start_checks does live GitHub reads (label_events approval, integrity, prerequisites), and `fa gate tick --no-start` records only 'starts-off' in flow-records. Bar = the facts replay `_admission` + name the live-read checks NOTE (#2125 r5, r810).



## Shortened index entries (11-06)



## Shortened index entries (11-06)
- Before stacking a feature on a merged one, run the new locked test's expectations against the merged feature's locked tests. Conflicts show up as concrete cases (#186 vs #786: claude 79% under keep 80 must win), and the reconciling rule (here: a written order beats headroom) follows from them.
- Before splitting a file for parallelism, read `overlap_per_file` in .foragent.yml (3 here) and the 'held:' reason in `fa orch facts`. 'cap' means the machine is full; a split cannot help. Also list the shared non-code files (docs/decisions.md, SKILL.md, config.py), which still overlap after any split.
- Repeated ordering holds over one cache row usually mean the row has two writers (ingest and local writes, as in issues.json + outbox_base). Propose: the latest observation has one writer, local writes go to an append-only intent log, and the visible row is derived. Check each locked test against that one rule. (#1125)
- A per-file hold is counted on *measured* claims (committed diff), so off-lane runs that touch a file for a few lines fill its cap (#1125, #1186 on order.py). A split cannot stop that. Before proposing a split, list the running claims with `overlap_files.claims(ctx, measured=False)`.
- In a design-first check, trace each reused function down to its git effects. `order.publish` commits and pushes in `_publish_files`, so 'reuse the publish path for issues' meant one push per order. Also, a per-key checkpoint file that gets overwritten fails whenever one key (boundary) can hold several batches.
- Before a 'split reads vs writes' option, check whether the locked test file mixes both (test_observation_ingest runs write cases through every read route); if it does, a split needs a locked-test re-cut, which is a person decision. Cutting an entity type that no locked case uses (comments, #1125) is cheaper.
- When matching a design to a locked test, also read the autouse fixtures in tests/conftest.py (no_forbidden_git_commands rejects any git argv with commit/push/reset/checkout/stash) and the test's IO probes (file_writes observes only Path.open/os.replace). Designs fail on these mechanics, not on the logic.
- A design that adds a multi-step recovery (rebuild = update-ref + read-tree + record) creates new interruption points that no locked test lists. Check each one: crash between moving a ref and recording it must be found by a durable mark (commit title), or the recovery repeats the effect. (#1312 r110)
- Before judging a feature's speed-up, check which `checks:` mode its code path and locked tests run in. #1139 group check returns early under checks: ci, and its locked tests are local only, so it ran 0 times in production (r185). Grep production logs for the feature's own log line to confirm.
- Read which argv a locked failure test injects into. #1208's test fails only a read and `gh pr create` before any effect, so composite-write findings (close+comment, merge --delete-branch) are above the locked bar. That makes it a scope question for the person. (r115)
- A path-disjoint collision count is only as good as its filter: r224's 0/333 counted agent-memory overlap as overlap, so it dropped #1062/#1180 from the sample. Before quoting a zero, list which cases the filter removes. Also label median and mean; a median quoted as a mean (16.4) went into A2's whole estimate.
- To model the merge queue, replay real `ready` times from merge-queue.jsonl with a fixed service time, and calibrate it against actual waits first (here ~20 min/24h, vs median 13.9). Split queue time into waiting and holding with phase events. Withdrawn entries waited more (6,898) than merged ones (5,879).
- CI comparison excludes every unmerged approval's locks, including the run's own (unmerged_acceptance_owners), and a test failing on base can never be lost. Green CI says nothing about Done-when; check the exclusions trailer on real PR heads (git log -1 --format=%B <head oid>).
- Compare review rounds with the last PASS, not only with round 1. 60 of 119 FAIL rounds came after a PASS, and 74% of findings on same-body rounds were on code unchanged since the PASS. To check, map heads by commit time from refs/pull/N in a bare clone, and read runs/issue-N*/review-R.txt.
- To test whether a held PR's machinery is above the locked bar, disable it in a temp detached worktree of the PR head and run its Done when (#1523: audit + Landing off, 12/12 + 15/15). The first run in a fresh worktree is cold (255 s vs 17.5 s warm), so do not read that time as a slow test.
- Before calling a ready issue stalled, read its `kind=ready` rows in `flow-records.jsonl` (state dir): `data.reason` gives slots-full, outbox-wait, blocked-by and so on, with `run_slots`. `fa orch facts` 'Parallel: free' is only the moment, so it hid an 8/8 then 9/9 machine for #1526.
- A reviewer finding about a stored or legacy format is checkable: count the construct in the approvals dir (body field, 405 files) and orders.jsonl, not only live issue bodies, and find the state file's single writer (order_submissions.retain). #1570 rounds 7-8 and #1523 round 10 had 0 real inputs.
- A Goal bar paragraph that names a finding class NOTE-only works, but with a lag: #1570 after r282 still took 4 rounds (FAIL 2/1/1, then PASS) and ~4 h before merge, and the PR grew only +428 -> +466. Expect 2-4 more rounds before judging a bar edit as failed.
- A fail-closed hint closes the class it names, and the findings then move to the one free-text slot the definition still allows (#1619 r319: exact permission text held; rounds 7-8 moved to the copyright-holder name). Name every free slot in the hint and say which ones are NOTE, or expect one more spec question.
- A conditional retry hint ('use if round N FAILs in class X') applies only at a retry boundary. Review FAILs inside a run are fixed by the executor without a retry event (#1619 r10 FAIL -> r11 PASS, no retry, r330 unused). Check `kind=retry` events before you count a hint as tried.
- Sort ownership or lock findings by end state before redesigning: an own edit whose bytes equal the approved blob is harmless, and a false hash-mismatch is a fail-closed stop. Only 'a run starts with bytes that differ from approved' is a bypass. #1679 r4 had 0 bypasses; both findings were outside the one locked case. (r342)
- A scope hint that the reviewer has already accepted often ends the holds before a conditional method hint is needed (#1652: r344's fail-closed hint was unused, PASS 1 round later). When the current round is still running, send the method hint as conditional and do not stack it on the scope hint.
- A value that a merge re-derives from inputs that can still change (preread history, base ancestry, refs) gets one hold per change path; keying it by approved body hash still fails when a later write shares the key (#1696 r1-r4). Hint: capture once at run start, first write wins, merge reads only the stored value. (r352)
- Before adopting a review NOTE from the prompt, read the issue's latest `parked`/`retry` events in orch-events.jsonl: the PM often acts on the same round first (#1208 round 9 notes at 06:17Z, parked and folded into merge owner #1502 at 06:21Z). A NOTE already answered by a park needs no proposal.
- A scorecard part fed by a newly written event (#1727 rework/real waste, 11-03) compares 24h against earlier days that have no such records ('unknown' 74142 before). So 'X rose 7x' is a coverage artifact until the 6-day window holds post-merge records. Find the merge that added the writer before you call a part worse.
- `fa orch facts` 'Binding: structure (fixable)' can be an intended `exclusive:` header hold. Read the `kind=overlap` row in flow-records.jsonl: `cause: exclusive` with one long peer is the chosen trade (r426), so measure it (held issues, minutes, new conflicts) and set a park trigger instead of a fix. (r450, 11-03)
- A run that times out in 'checking' may run tests twice: Done-when unit files that are own tests of Files are re-run by the related check. Measure with runs.jsonl related_tests[].file_seconds vs bare test paths in session_body (10-28..11-04: 16,981 s, #1641 7,320 s). (r497)
- A `kind=overlap` flow-records row with `data.holds: []` is a check with no wait (#1655, 11-04); count only rows with holds before citing overlap waits. New files in hot_overlap_files (init cluster, r511) are co-presence too, so check conflicted overlap_records first.
- lane_concentration counts each retry as its own execution, so a CI-timeout storm inflates one lane's share. Count executions per distinct issue and the 'CI did not finish' outcomes per hour in runs.jsonl before reading it as scope (r570, 11-04: orch 25 runs on 13 issues, 15 were CI timeouts).
- A run killed/stopped long after start may be a ghost: its issue was closed outside the gate (PM direct merge via group PR) and the run never re-reads issue state. Compare runs.jsonl started_at/finished_at with GitHub closedAt (11-05 #2100: 121 min after close; r787).

## Locked snapshot of a moving file (r913, 11-06)
#2209's locked test hard-codes KNOWN = the 75 tests of tests/test_merge_owner_batch.py at order time. The issue passed 5 times but stayed in the CI-wait loop; meanwhile 3 main commits added 9 batch tests, and after the rebase the reviewer held twice on 'new tests lack KNOWN entries'. The executor cannot edit locked tests. Check: compare the snapshot's names with origin/main's file (ast). Fix: a Goal bar sentence that makes post-order additions NOTE-only; a hand refresh goes stale with the next commit.
r901 measurement: D89 (#2383, silence-based CI wait) dropped 'ci -> timeout' from 55 (06:00-17:41Z) to 1 (17:41-20:19Z) with no shard rebalance.

## CI-wait fix vs job runtime (r901)
Measure completedAt-startedAt of the slowest unit job per green run; 11-06 12-17Z 25/42 were over 1200 s, so measuring from last job start (r875/#2343) could not clear the loop. JUnit per file: `gh run download <main run>`.

## Full text of shortened index lines (11-07)
- A 'Broader class' review-note set that is 4 of 4 FAILs in a round can be a pre-existing main defect held via pass_scope_exempt (#1514 r15: _remove_run_worktree finally, on main since 10-24). Check `git blame` and the PR's diff of that file, then propose its own issue + out-of-scope hint (r837). Reviewer final text is in logs/issue-N-review-R-P.log (last json lines), not runs/issue-N/.
- A 'process holds state.lock on two fds' claim from lsof is not evidence of self-nesting: lsof 'w' is the open mode, each thread opens its own fd, and macOS cannot show flock owners. Read the `sample` per thread and find which process breaks the cycle when killed (11-05: tick facts _freeze state SH -> capacity vs admit_run capacity -> state, r821).
- 'CI ran past the wait limit' on green heads: read `gh pr checks N --json name,state,bucket,startedAt`. A SKIPPED row (acceptance-base on PRs) carries run creation time and QUEUED rows carry a non-zero startedAt, so min(start) counts queue time. Compare per-run 'after last job start' with the 1200 s limit (r875, 11-06: 16/53 vs 2/53).
- A per-base-sha CI record whose producer takes longer than the main push gap cannot hit (11-06: 65 min producer vs 1-min gap, 3/63 hits even before cancel-in-progress). Check whether the comparison needs the record at all: lost = base_pass - head_pass needs base only for head-non-passing nodes (r893). Read origin/main, not the local checkout, for workflow files.
- A facts 'files changed' in the tens of thousands is an untracked scratch dir in the worktree (#2172 .tmp-2172 for cxdo, 39k files); git.stage_changes stages all untracked except `.foragent`, and a root `train-scope-proposal.patch` already reached main via #1970. Check `git ls-files` at repo root. (r832)
- A PM-filed 'Review follow-up for #N' issue does not stop holds on #N: the reviewer keeps FAILing that class until #N's Out of scope names the follow-up (#1514 r17 vs #2230, 3/3 FAILs; same round accepted the #2219 hint). Check `gh issue list --search "Review follow-up for #N"` first (r860).
- If `fa orch facts` itself crashes, that is the review's finding: 11-06 #2267 lock order ranked str(path) ('-' < '/') while _freeze sorted Path parts, so foragent vs foragent-sandbox raised 'Lock order inversion' (r865). Check `ls ~/.local/share/foragent` for prefix-named stores.

## r939/r940 (11-07): repeated-class review hold survives a same-body retry
run_review._review holds when review_scoring.repeated_classes(n) is non-empty; failed_rounds counts FAILs per approval body_sha and clears only on PASS. The PM question (q44 'Ship with follow-ups / Narrow the scope') has no reader, so a retry without a Goal edit implements+checks (~67 min for #2312) and holds again with 0 reviews. Reproduce read-only: tool-versions/current/tools/foragent/bin/python3 -I script calling repeated_classes(n, SimpleNamespace(store=StateStore(root))).

## Full text of shortened index lines (11-07)
- A feature that repeats the reviewer hold "found defects after 3 rounds" with a new race each time usually has a Goal that sets a higher bar than the path it reuses. Name the inherited races as non-goals. Do not add more ordering rules. (#370, 9 holds)
- Lanes do not limit parallelism here: no per-lane cap without a live plan width, and starts are ranked across lanes. A "redraw lanes to spread work" question is answered by checking binding (`fa orch facts`) and the per-lane open backlog first.
- In CI mode a test-slot or baseline-wait spike usually means some runs targeted a non-base branch (ci_mode.enabled needs target == base). Group phase events by hour (orch-events kind=phase, seconds) before blaming test count. (10-31 batch night)
- `fa orch facts` scope drift flags about 95% of runs and its printed examples are agent-memory paths; only 89 of 654 are memory-only. Subtract memory paths before citing it, and compare `changed_files` with the PR's files (they matched on the last 12 merges).
- A person-decision proposal parked for a later trigger goes stale while the run continues (r296: 'findings did not fall', then round 17 went 7 -> 3). Do not write a trend claim while a round is running; state the two counts and let the trigger re-read them.
- A protected-check timeout that reads 'allowed 0s' means the run had used its whole budget (`_protected_limit` = min(adaptive, runner._remaining)). It is not an adaptive-limit bug. Read started_at in runs.jsonl before you blame the limit. (#1641, 11-02)
- Count each round's findings by fact or class (#1469 waiting_on_person 1/5, 1/3, 1/1, 3/3). When one class grows to the whole round and no locked case covers it, propose moving it to its own issue with a locked test instead of another hint.
- files_over_limit/hot_overlap_files/lane_concentration re-fire on unchanged 24h counts (r392..r589). Read the last answer's re-check trigger (r389: 3+ conflicts/file/48 h), count conflicted overlap_records since it; unmet means answer with the count.
- 'CI did not finish' can be GitHub run finalization lag: compare each check's completed_at with the workflow run's updated_at (normal lag 1-6 s). On 11-04 15:50-16:11Z it was 10-23 min, and #2014/#1854 timed out with every job green (r682).
- A 'Stale acceptance shard' held outcome after #1925 (merged 11-04 18:28Z) is the refresh-once path: #2036 went held 22:14Z -> baseline -> ci within 5 min. Read the issue's next phase events before counting it as a stall (7 in 7d, owned by #1922).
- Classify long checking by trigger (first / after FAIL / post-PASS base refresh / after CI): walk orch-events phase rows per issue, attribute files via runs.jsonl related_tests (dedupe; rows repeat). 11-05: 4 triggers ~even, one file 67% (r790).
- `outbox-wait` is a global start block (scheduler_start._start_ready: any undelivered GitHub write holds every candidate). 29 waits 11-05/06: median 8.6 min; the 315-min tail was the 16:58-22:04Z deadlock. A fresh burst after PM retries is not a case (r844). Re-measured r1072 (11-07): 32 waits/24h, median 8.5 min, max 13 min; a PM publish burst mid-tick re-queues the outbox and holds the earlier batch one more tick. For the 24h worst case, look past outbox-wait to file-overlap rows.
- A hint proposed while a review round is still running can be overtaken: #1514 r17 had 3/3 FAIL notes, then PASS at 04:46 with no hint (r860->r862). Before a held-issue hint goes out, check `review-pass-head` in orch-events; an edit after PASS stops a run in CI.
- Before proposing to fit work under a fixed limit, search open decisions/issues that remove the limit: r901 (balance unit shards under the 1200 s CI wait) was superseded 30 min later by D89/#2383 (CI waits end on silence; 'ci -> timeout' 55 -> 1). Slowness then belongs to the test-split issue (#2209), not the wait.
- Scorecard overlap-wait minutes = sum of orch-events `kind=phase, phase=overlap-wait` seconds; list them by issue first (11-06: 130.5 min was one row, #2078 size:l 45 files, the waiter not the blocker). runs.jsonl keys the issue as `number`, not `issue`. (r916)
- A wide-first hold (overlap_wide, #2353) matches the wide issue's *declared* Files globs, not its committed effective_files as claim() does; one unused glob (#1514 messages/**) held 3 view issues (r926). Check runs/<n>.json effective_files vs the held file.
- A wide-first hold whose peer shows `run_started_at: null` is not a stale run: overlap_wide holds while the wide issue is merge-queued too (no active run), so the hold ends at its merge. Check the wide issue's last phase event before calling it a ghost. (r931)

## Idle lane with PASS'd ready work: check for a retry of merge-queued work (r943, 11-06)
When a lane's ready issues show `kind=retry` shortly after `ci -> merge-queued`, the retry probably came from a person/PM loop with a stale list. `_retry` calls close_for_repair on any open PR, so the active batch then retires with 'single path final check held'. To find who ran it, grep `~/.claude/projects/-Users-apple-Dev-foragent/*.jsonl` for `retry <N>` (orch-events has no caller). Check the head's green CI with the check-runs API.

## r950 (11-06 22:40Z): a repeated_design_holds alarm right after a diagnosed cause
Before you dig, grep `fa orch inbox` for the issue numbers and for the latest `not-now` answer on the same signal. Then check `gh issue list --search` for the fix's plan issue. r949 (#2215/#2312) was r940's cause, already published as #2418. The only new fact was the baseline count (2 wasted runs). Send a measurement note, not a new proposal.

## repeated_design_holds counts pre-review repeat holds (r969, 11-07)
The gate's repeat check (runner._execute / run_review._review entry, HOLD_REASON) flags Reason.REVIEW_DEFECTS, pinned by locked test_review_decision_hold_reader. So each same-body retry of a held issue adds one 'design hold' with no reviewer verdict. On 11-07 the whole alarm ({#2209:2,#2215:3,#2312:2}) was made by 4 zero-review runs; counting only runs with a `kind=review` event in [started_at, finished_at] gives {}. Replay: runs.jsonl held rows x orch-events review rows by issue and window. Proposed r970 (count only reviewed runs) and r971 (skip retry-refusal; #2418 already holds before baseline).

## Publish -> main path survey (r985/r986, 11-07)
- order-publications/*.json are rewritten atomically, so st_birthtime == mtime on single-record plans. Measure publication latency per `plan.id` from the first record of a supersede chain to the `published` record (a lower bound), not per file.
- A wait rule 'block until no train' costs a whole train (~55 min CI, trains back to back). A wait limit shorter than the thing it waits for (flush 40 min vs train ~52 min) times out by design. Compare the limit with flow-records `kind=merge-batch` ci_minutes before you blame the timeout.
- Classify `git log --first-parent` subjects before you count 'main writers': 11-07 24h = 163 commits, 61 role memory, 27 direct flush orders, 21 direct PR merges. D118 'one writer' did not hold.

## Train 'tip CI red' = acceptance shard timeout (r988, 11-07)
A merge-batch row with reason 'tip CI red' may contain no failure at all. Check `gh run view <id> --json jobs`. A cancelled 'acceptance (N)' job whose annotation says 'exceeded maximum execution time of 30m0s' is a shard overload. Each shard runs base and head passes when the base cache misses, and it almost always misses because main runs cancel each other. Shard load comes from scripts/shard_times.json (hand-refreshed; 11-07 it timed 321 of 591 files). Untimed files go to sha256(path) % N. Count untimed files before you add shards: hashed max/mean is ~1.57 at both 4 and 6 shards.

## Solo remainder after a red singleton half (r991/r992, 11-07)
In an idle_lanes alarm whose held issues wait on a merge-queued wide peer, read the `kind=merge-batch` rows. After [A,B] red -> [A] red (A spent), `_failed_candidates` reserves the whole remainder [B] as a train of 1 (`(retry, 1)` breaks before `(eligible, 2)`). PASS'd members queued meanwhile wait one extra ~45 min train. Reproduce read-only by calling `_failed_candidates` with stub ctx/rows (SimpleNamespace candidates `(p, j, pr)`). In 2 of 2 cases (11-06 #2272, 11-07 #2322) the remainder was innocent.

## Accepted is not ordered: a facts fix that stays unordered re-fires the alarm (r995/r996, 11-07)
r970 (count only reviewed holds) was accepted at 00:31Z but not ordered. 2.5 h later r995 fired on the same three issues, plus #2454. #2454 had 1 real reviewer hold and 1 pre-review repeat hold of 7 s (#2418 works). Before you dig into a repeat alarm, check whether the accepted facts fix has landed: `git log -- src/foragent/orch_facts_kinds.py`. If it has not, send a measurement note that asks for the order. The replay script is in r969. Dedupe runs.jsonl rows by (number, started_at), because rows repeat per status write.

## Post-CI 60-min baseline = workflow-changed fallback (r1018, 11-07)
A phase=baseline event with event_id `baseline:` (not `baseline-local:`) right after `ci -> baseline` is ci_local.fallback_comparison, a full local acceptance capture. 11-07 #2449/#2172 hit 3,600 s (exit 143). Trigger: main edited foragent-acceptance*.yml (af5c60c9, 38c52083) and changed_workflows diffs trusted..head with two dots, so a head that is only behind counts as "workflow changed". The run log prints no reason line; match ci->baseline times against `git log origin/main -- .github/`. Full capture durations are in ../baselines/*.json (duration_seconds, timed_out): 1.5k s 10-31 -> 3.1k s 11-05 -> cut 11-07.

## Sweep-to-order coverage (r1019/r1020, 11-07)
When mapping defect-class sweeps onto a drafted order, check each class guard's *verb* against the incidents: one-path's lock guard faked only network calls, but the measured route.lock timeouts came from pytest collection under the lock. Also time each 'full read' before calling it a defect (merge-queue replay 0.055 s/1,135 rows); its real issue was size (publication copy = 99% of 32.7 MB).

## Publication lands before a queued member (r1023/r1025, 2026-11-07)
merge_owner.tick runs order_publications.apply_next before _tick (merges), so queued publications always land on main before merge-queued members. When a member deletes what publications write (#2161 removes docs/specs; main's publish still writes docs/specs/<id>.md), the train tip gets the files back and 'Train tip Done when' fails (03:21 and again 05:xx on 11-07). Check: `git show --stat <main commit 'batch owner publications'>` against the queued PR's diff, then test the member's Done when on the train branch tip (`foragent/batch-merge-<base>-N`).

## hot_overlap_files is always on; propose a lost-work predicate (r1031/r1032, 11-07)
Hourly replay over 7 days: the co-presence predicate (3+ runs per file in 24 h) was met 168/168 h. "3+ issues with a conflicted record" was met 91/168 h, and every such case was answered "nothing to change". redo_count > 0 was met 0/168 h. redo_count is written only by run_conflicts._rebuild (a rebuild after the conflict budget is used up), so it is the real lost-work signal: 0 of 5,296 records. runs.jsonl keys are `number`/`started_at`, not `issue`. Phase overlap-wait events carry `seconds`. Before you answer another hot_overlap_files alarm, check whether r1032 (fire only on rebuild or conflict stop) has landed in orch_facts_kinds.py. If it has not, send a measurement note that asks for the order.

- lane_concentration re-raise with lane_runs unchanged (r1092: 13/25 -> 13/23) is the 6 h window dropping other lanes' runs; orch_role_raise.worse counts any SHARE_FACTS rise. 17 of 39 raises 10-31..11-07 were like this. Check lane_runs against the previous raise before reading lanes again; proposal r1093 (worse only if lane_runs rose; locked test cases keep lane_runs +1).

- Locked acceptance tests (`tests/acceptance/*`, "do not edit") constrain every later proposal. Check `grep -ln` there before you propose a behaviour change; unit tests may change.
- Real line-limit pressure shows up in `fa orch scorecard` repeat failures (locked `test_*_split.py` limit tests), not in the 500-line files_over_limit signal. Splits of over-500 files with 0-2 conflicts and redo 0 have shown no gain.
- Goals grow 'Fixed answers'/'More:' clauses after each hold; when those clauses need to know which turn/attempt/snapshot is which, replace them with a value derived from an append-only record (#238 turn ordinal) instead of another rule.
- A spec conflict between a locked test and 'needs a sample first' is often a missing definition (what is the reference with no sample?), not a real conflict; answer it with the definition (#435: run start).
- A busy lane rarely dreams: dream_due needs the lane idle. Check `runs/dream-<lane>.json` and orch-events dream-done before blaming notebook size on lane width. Dreams take ~2.5 min, so the start hold while dreaming costs little.
- An 'approval carries hashes' idea fails while approval is a GitHub label: the hash record becomes a second writer next to the label event. Treat changing what an approval is as a person decision.
- To see what a limit change would free, replay admission read-only: patch `orch_plan.file_limits` and run `cli.main(['orch','facts','--json'])` with the fa tool-versions python (the repo python has no foragent module). Read `held`.
- Check merge wait in `fa orch scorecard` before freeing overlap holds. On 10-31, merge was 4792 min/24h against overlap 587, so the freed runs mostly queue at the merge lock.
- A dedupe that reads a second record (last comment text, park history, outbox) gets one new hold per path. The fix is 'act iff the observed state before differs from after', with the state read at the start as the only input. (#1186 r114)
- GitHub native merge queue needs an organization-owned repo; this repo's owner is a User. In CI mode one merge costs one fresh CI round (~8 min) after each main move; the merge lock is held only ~0.6 min.
- Per-test durations live in runs/issue-*-acceptance-baseline.xml (JUnit) in the state dir; fa orch facts slowest_test_files are wall time inside runs (3.7x the solo time under 4 slots).
- Main red episodes here mostly start on order commits that touch only docs/specs or tests/acceptance, so 'last commit = culprit' is wrong; any auto-revert design needs culprit confirmation and flake fixes first. (r224/r225)
- Count mechanisms against saved work before tuning them: refresh in CI mode runs local checks and CI both, and a 'neutral' move still pushes and restarts CI, so it saves only the local part (run_publish_flow refresh_base/publish_refresh).
- Order commits land on main more often than merges (281 vs 235/7d, mostly unbatched). Any 'base moved' cost or evidence window must count them; `git log --first-parent` with a window test gives the exposure.
- An ordering of the run's steps can make a new feature unnecessary: _wait_for_ci precedes await_turn's join, so making CI red on lost gives pre-queue detection with no new reader. Read the existing step order before adding a reader.
- Before blaming a phase total, group it by hour. The 10,406 min merge wait of 11-01 was 775/h until 13Z and 33/h after; the median merge gap never moved, so the queue drained because arrivals fell.
- Role wakes follow each finished order (D99); Jin rejected count or time thresholds for them. A wake soon after the last review is a light check of that order's diff, so 'Nothing to change' is the normal result; do not propose a threshold.
- List a held issue's closed PRs with sizes vs PASS rounds; #1158 grew +242 (PASS) -> +980 on one Goal, so recover from the PASS head + Goal edit. Compare Goal text via GraphQL `userContentEdits`.
- A Goal non-goal naming one class works (reviewer wrote 'r85 rebuttal accepted') but findings move to the next class (#1004 rounds 13-15). State one bar: what the reviewer holds on, everything else is a NOTE.
- Split review FAILs into before and after the first PASS: post-PASS scope (#1599) leaves pre-PASS 4th+ FAILs (140 of 509/7d) untouched. Measure one change before stacking another review rule.
- For "tests check internals" counts, use ast: private-name access is 15% of unit tests but 2% of locked tests; the locked cost is cross-imports (43 unit files `from test_X import`), not private access. (r432)
- To measure a reader's cost, count real file opens (wrap open/read_text, attribute by traceback) in `gate watch --once`; 11-04 runs.jsonl was opened 26x per frame but one parse took 0.16 s. state_logs.py is the single log deleter. (r486)
- lane_concentration counts run *starts* in 6 h (CONCENTRATION_WINDOW), running included; a +1 is a new start (r714: #2035), not a finish. List starts by lane, not finishes.
- Each conflicted overlap_record appears once per side (#1915 and #1515 both list state_runs.py), so 48 h row counts double. Count distinct (file, issue pair) events before testing the r389 3+/file trigger (r678: 4 rows = 2 events).
- lane_concentration 'idle lanes': check whether their approved issues already have open PRs (gh pr list headRefName); if so they wait on merges and the alarm is merge_stall, not scope (r725: 15 of 17 had PRs).
- A 'role ran a person command' incident may be shell command substitution: backticks inside --text "..." ran fa stop x3 (11-05, r768). Scan tool_use commands in logs/<role>-*.log first.
- 'Fix merged but it did not fire': a run keeps its start-time tool version (`tool-versions/<ts>-<sha>`) ~5 h; compare started_at with the first install dir containing the fix (r772).
- facts '기준선 캡처 ... 한도에 가까움' is the latest long baseline, not a trend: list phase=baseline >600 s by issue (11-05: median 30 s; only #1422/#1923/#1915, #1422 1692->2725 s).
- Before reading a 6 h lane_concentration count, list tick gaps in flow-records (`kind=tick`): after a gate stop (11-05 16:58-22:04Z deadlock) the window holds ~1 h of live gate, so few starts and one lane's share look large. (r827)
- A retry-hold release keyed on approval time or whole-body hash fails review (#2458 r1-r2); key it on the contract: Goal-section hash + acceptance_blobs stored at hold time (r1008).
- blocked_by gates only the start (scheduler_start.py:298); FA has no merge-only edge. To make step X merge only after test S passes, put S's file in X's Done when. A test-only issue can gate X only if something other than X makes it green (r1029).
- Before you cite a scenario issue as red, run its locked test on origin/main in a temp detached worktree. #2452 S1 was already green on 11-07 because another fix had merged, and as an approved unmerged owner its lock was excluded from every other run's comparison (r1029).
- runs.jsonl writes a finished run 1-4 times (11-07: 1,296 rows = 862 runs in 7 d); dedupe on (run_generation, number, finished_at) before counting. facts already does (859).
- facts '기준선 캡처: 최근 N s ... 한도에 가까움' reads `latest_success` (run_baseline_limits.py): timed-out captures are skipped, so 3118 s (11-05) keeps re-firing after the r1018 cut. Not a new event without a new successful capture.
- A PR that hand-writes a tool's rules (gitattributes wildmatch in #2542) often answers a locked subprocess allowlist (test_flow_scorecard_split.py:157 lacks check-attr). Grep the locked guards; propose a one-word lock fix (r1069).
- 'X% of each test is setup' must be measured with --durations (setup vs call) or cProfile before a shared-fixture order: #2209's shared start (r760 premise 80% setup) left the merge-owner batch tests at 1,675 s serial / 2,326 s per related check, because ~990 git calls per test happen in the body (merge_owner.tick). r765 expected <400 s: not met (r1110, 11-08).
- Related checks run all selected files in ONE serial pytest under one test slot (run_checks.run_related_check; no xdist installed). Since 11-01, merge_owner* files were 52% of related-check seconds; a greedy N-bin replay of runs.jsonl file_seconds gives the parallel ceiling (-30% at N=2). (r1110)

## repeated_design_holds after #2501: legacy rows (r1152, 11-07)
#2501 skips a hold only when a `repeat-hold` orch-event matches (issue, started_at). Runs before its merge (11-07 06:36Z) have no such event, so their 0-review repeat holds stay counted until 11-14. They do not re-fire alone (the gate re-calls only on a worse burden). For a new alarm, look only at the keys that grew; for #2560 the orchestrator's PASS (round source=orchestrator) had already ended the holds.
