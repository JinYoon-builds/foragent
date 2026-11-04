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
