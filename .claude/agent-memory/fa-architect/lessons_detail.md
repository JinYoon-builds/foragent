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
