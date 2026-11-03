---
name: pending-after-merge
description: Orchestrator follow-ups that wait on a specific merge (check on every merge event); 2026-10-30 list
metadata:
  type: project
---

- Merge groups brought forward by Jin (2026-10-31): #459 is being replaced by a split order (drafts/split459); closing #458/#459/#460 is a proposal for Jin via the planning session.
- All dream PRs closed by Jin 2026-10-31 (#898, #901, #940); confirm order, view and orch lanes re-dream when a slot frees (fa orch log dream-done / lane dreaming in gate status).
- Pushes: Jin turned them off on purpose (notify: none, me scope, 2026-10-31 00:00); do not 'fix' that. Keep `repo:` in .foragent.yml.

- PR #1224 merged; #997 edited (no D99 dup), r62/r63/r82 closed. #1025 PR #1120 is conflicting on decisions.md: when #1025 runs again after #1125, check its rebase.

- DONE 11-01: #1125 merged; told the PM to re-preread/approve blocked-copy and #1025. PM approved #1182 and #1025; #1025 unparked + retried 11-01 (behind #1141/#1403 in the global queue).
- #1025 split: when #1125 (observation-ingest, board) merges, tell the planning session; it re-runs #1025's preread and approves; then unpark/retry #1025.

- After #997 merges: #1007 (dream writer) re-approve unchanged (its shared test test_team_v1_dream.py changed with the D99 order) and unpark/retry.

- Note: `fa order publish` without --approve on an already approved issue removes its approval; always re-approve edited approved issues in the same step.




- Orch lane split (2026-10-31, Jin option A): on or after 2026-11-07 measure (1) wc -l .claude/agent-memory/fa-*/MEMORY.md (all <=200), (2) open issues in the largest lane (~15), (3) conflict/wait minutes in fa orch scorecard (expected unchanged). Relabels of #1172/#1173 after their runs (planning session does relabels).

- Jin priority (2026-10-31): global queue front = merge-group chain #1140→#1141→#1142 and review-direction chain #1167→#1168·#1169→#1170→#1171. After each merge in a chain, check the next one starts (held reason should be cap only); delete when #1142 and #1171 merge.
- #1043 parked 10-31 (Jin: split into 4, Codex draft scratchpad/split1043). If pieces 2-4 hold twice for one class, ask planning about cutting scope. Close #1043 via replaces after the split publishes (planning asks Jin).

- #1105: architect called 08:25 (2nd hold, command-rewriting class); apply its Goal redesign.
- One-entrance order: when planning sends the github-writes init-fa-allow number, add it as blocked_by of the hidden-orchestrator issue.
- #1269 wheel-scroll (bug, preread clean, unapproved): approve with drafts/wheel-scroll/plan.json once #1062 and #1064 merge (same view files).
- d85-split: after planning publishes, send batch-flush/flush-recovery preread designs to the architect (design-first trial); report to planning; count their returns vs 5-9 average.

- #1125 under r81 design: 2 holds of one class (paths bypassing the single record: reads, then writes/comments). Architect was busy (#1264 check): call it for split/cut options as soon as r-reply for #1264 arrives; send its option to the PM for Jin (scope decision), then edit+re-approve.

- #1160 parked 10-31: test_presence.py 4 tests fail on this machine on main (host-dependent; CI green) and the related-unit check blocked it. Fix order drafting in scratchpad/presence. After the fix merges: re-approve #1160 (edit_plan with empty append + preread), unpark, retry.

- #1295 (test_presence time bomb, fails all CI since ~10-31 12:00Z) and #1296 (related-unit base comparison) approved at queue front. After #1295 merges: retry #1062 (PR #1278), #1049 (PR #1277), #1184 (PR #1280), #1296, #993 (acceptance shards missing while CI is red), #1141 (also a CI-only test_lane_talk pause failure that passes locally on main, branch and merge; if it repeats, it is order-dependent: order a fix) and any other issue blocked by test_presence CI failures.

- #993: 3rd hold 10-31 13:0x UTC (record status treated as live reservation) -> class hint. If held again for that class: call the architect (check-then-act on slots).

- Overlap 6 on order.py, commands/order.py, tests/test_order.py, docs/order.md until 2026-11-02 14:13Z (r88). Check fa orch facts: revert (PATH=off) if >=2 conflicts or any redo on them; expect structure-held 8 -> <=5.

- (ended 22:00Z, review fast again) review=batch since 2026-10-31 15:19Z (25ebbde5, Jin via PM): only #1295 -> #1141 -> #1142 merge, by the PM with fa orch merge. Tell the PM each PR number when review+CI pass. After #1142 merges and the gate runs the new version: fa settings set review fast (+ commit/push .foragent.yml from a temp worktree), check held PRs drain, report the first batch merge.
- #1324, #1336 approved 10-31 ~16:30Z. #1337 (meaningful signal change) unapproved: Codex correcting older locked signal tests (scratchpad/reqanswer fix.out); then republish drafts/orch-request-records plan, preread, approve.

- DONE 22:00Z: review set back to fast (f6969579) because #1142 was not merged. Old note — Overnight guard (Jin via PM, 10-31): if #1142 is not merged by 2026-10-31 22:00Z (KST 11-01 07:00), set review fast (commit/push .foragent.yml from a temp worktree). Send #1295/#1141/#1142 PR numbers to the PM as soon as each reaches the inspect review group. Collect choice questions for Jin's morning; do everything else.
- #1264 and #1312 passed the design-first check (r105/r110); PM approves.

- #1208 parked 10-31 16:0xZ for Jin's scope choice (architect r115, rec A: retry reads and read-checked single writes only; Goal text in r115). On the answer: edit Goal, re-approve, unpark, retry on a new branch; close r115.

- Defect to order (choice-free): a run that waits hours for its merge turn times out with 'Too big for one session; split it' (#1233, 10-31 16:1xZ, waiting since 10:06Z). Merge-turn wait should not spend the run budget or claim the work is too big.

- test_slots ceiling 5 -> 4 (10-31 16:5xZ, 9cb45621 + machine.yml) after 3 check timeouts at load 1.15/CPU. Check later: check timeouts stop? test-slot waits grow? Raise back to 5 if no timeouts and slot wait dominates. 18:2xZ: #1233 timed out again at 78% with 4 slots (last capture 1706 s, limit grows 2x). ORDERED #1347 (900 s fixed limit at run_checks.py:494). After it merges, consider raising test_slots back to 5.

- DONE 11-01 02:4xZ: #1345 merged; #1337 re-approved, unparked, retried. Old note: a run reuses an existing issue branch created before the approved acceptance files changed (foragent/issue-1337 at 01:12, no own commits), so every check hits hash-mismatch and re-approval loops. A run should start from or update to the base when its branch's locked files differ from the approval. If the hint retry for #1337 fails again, order this first.

- Environment note (10-31 19:1xZ): Codex-executor runs cannot write ~/.local/share/foragent/test-slots.lock (sandbox), so `fa test` fails for them (#1322). If it recurs on another Codex run, order: the executor sandbox gets the slot lock path writable, or fa test degrades to a plain run when the lock is not writable.

- Possible deadlock (10-31 19:5xZ): full protected-acceptance verification (~1600 nodes) exceeds the fixed 900 s limit; only small selections (52) pass. #1347 fixes it but the gate runs the installed old version, so #1347 may time out too. Asked the PM for Jin's morning choice (rec: Jin hand-merges #1347's PR on GitHub). Send #1347's PR number as soon as it exists.

- DONE 11-01 00:4xZ: gate reinstalled with #1347 (da17f980, via the #1326 merge); all 21 held issues retried.

- #1347 implemented on branch foragent/issue-1347 (c0b129a9, WIP commit, +123/-6 in run_baseline.py, run_checks.py, 2 unit test files; 144 tests pass locally) but blocked: the installed gate cuts its own protected check at 900 s. No PR exists. Jin decides (morning) how to land it.

- #1347 follow-ups (PM review of branch c0b129a9, 4 P2s): Codex drafting on the #1347 branch (scratchpad/after1347). QA, save to drafts/adaptive-limit-followups, and publish+approve right after #1347 merges; queue the group-check issue before #1141/#1142.

- #1341 merged 10-31 ~00:2xZ (human premerge now excludes pending approved locks). PR #1298 can be merged by the PM again.

- Jin chose hand-merge (11-01): PM opened PR #1356 from foragent/issue-1347 and will merge it on GitHub after CI. Do NOT retry/park/touch #1347 meanwhile (it is blocked+needs-human, no live run). After the PM says merged: publish+approve drafts/adaptive-limit-followups (group-check first, before #1141/#1142), then retry the held list (re-approve #1179 first).

- #1105 blocked 11-01 00:2xZ: PR body over GitHub's 65536-char limit. Fix order drafting (scratchpad/prbody, key pr-body-limit); publish+approve it, then retry #1105 after it merges.

- Defect to order (choice-free): after a merge done outside FA (GitHub UI, e.g. PR #1356), the gate never reinstalls from main (only orch_reinstall.after_merge on FA merges), so fixes do not take effect until the next FA merge. `fa update` installs release tags, not main. Waiter b6phaazzf watches for a tool version containing 732ce339; then retry the held list.

- Jin chose A (r185, 11-01): #1142 parked (unapproved, not closed; decide after 7 days of group failure rate, ~11-08). #1141 kept. Draft saved at drafts/ci-group-evidence and sent to PM (PM publishes+approves, adds blocked_by #1141).

- #1141 merged 11-01 03:16Z (PR #1407); PM approved #1403 (queue front). Hand-merge it like #1141 (find PR by --head foragent/issue-1403).

- Hand-merge plan (Jin via PM, 11-01): #1141 and #1403 do not take the merge queue. When either reaches merge-lock-wait with review passed and PR CI green: send PM the PR number + head SHA, then `fa orch stop N --keep-work`. After #1141 merges and the PM approves #1403, queue #1403 first in its lane. After #1403's hand-merge, confirm the gate reinstalls on the next FA merge and report the first group (merge-queue.jsonl members > 1).

- Overlap 8 on #1403's files (ci_mode.py, ci_workflow.py, foragent-acceptance.yml, test_ci_mode_unit.py, run_group_*.py, run_publish*.py, tests/test_run_group*.py), expires ~11-01 09:3xZ. Remove them (PATH=off) right after #1403 merges.

- drafts/merge-group-auto/plan.json belongs to the PM (Jin approved the card). Do not touch or publish it. When #1403 merges, tell the PM; the PM publishes+approves it then.

**Why:** these were promised in request notes (r48, r28); a restart loses them otherwise.
**How to apply:** on each merge event, check this list; delete lines once done. See [[pending-drafts-not-in-scratchpad]].

Since #996 (merged 2026-10-31) `fa orch park` removes approval itself and a parked issue needs explicit re-approval to run; the manual label removal used for #459 is no longer needed.

QA lesson (2026-10-31): check every Done when command in a Codex-drafted plan uses `uv run pytest` (this machine has no `python` on PATH; the review-direction plan shipped `python -m pytest` and stopped #1172).

Fact (2026-10-31): blocked_by lives as GitHub issue relations created at publish; the gate reads them (scheduler_checks). Edits cannot change them, but existing relations still hold, so an approved issue waits for its blocker. Check with gh api graphql issue.blockedBy before assuming a dependency is missing.

- Subtraction survey DONE 11-01: r201-r206 forwarded to PM. Mine: S1 r201 (cache CI base passing set per base sha+shard) - order right after #1403 + merge-group-auto merge (workflow file overlap); S3 r203 timeout-outcome fold + orch_scope aliases after #1004; S6 r206 D15/D06/D04 - subtraction period. Person (PM asks Jin): S2 batch delete (rec A), S4 group test files after #1142, S5 delete docs/specs+impl. (Was: architect called 04:04Z, read-only, 5 areas; SSOT report copied to drafts/subtraction-survey/ssot-report.md. When its proposal lands in fa orch inbox, send the result to the PM (Korean). Jin picks scope; work starts after current issues.)

- test_slots stays 4 (11-01 04:30Z check, PM asked): at 4 slots during the 10-31 17Z-11-01 01Z burst cpu_busy 81%, load 0.93/CPU, swapins 3,233; 16 GB with 675 MB unused. Slot wait was that burst (21 retries after #1347), 0 since 02Z. Raise only if a busy hour shows cpu_busy < 60% and no swapins (budget-history.jsonl under ~/.local/share/foragent).

- #1403 MERGED 11-01 ~04:40Z by PM (PR #1413, direct). Overlap-8 raises removed. Still to do: confirm gate reinstalls on next FA merge (installed build must contain #1403's commit), report first merge group (merge-queue.jsonl members > 1) to PM. PM publishes merge-group-auto now. S2/S5 drafts being written by cxdo in mainchk/_drafts -> copy to drafts/batch-delete, drafts/specs-delete, send paths to PM (PM approves, publishes after merge groups are on).
- #1415 (merge-group auto) merges through FA normally — do NOT stop it at merge wait (PM correction 11-01: Jin's hand-merge permission covered only #1141 and #1403). It is first in the global priority, so it takes the next merge turn.
- S2 drafts/batch-delete and S5 drafts/specs-delete: preread clean 11-01 ~05:10Z, sent to PM (PM approves+publishes after merge groups are on, and after #1372 and #1312 merge — they lock files S2/S5 rewrite). Order: S1 (mine, after #1415) -> S2 -> S5.
- Jin approved S2+S5 cards (11-01). PM publishes; I must not touch drafts/batch-delete or drafts/specs-delete. Tell PM when #1372 merges (S2 can go; also retry #1105) and when #1312 merges (S5 after S2).
- Facts defect to order later: 11-01 05:50Z `fa orch facts` said "Binding: test_slots (fixable) ... with machine headroom" while its own sample showed CPU 93%, load 1.47/CPU, memory warn (swap 14.7 of 15 GB). Headroom judgement ignores the sample.
- Gate defect to order later: the same architect role signal (hot_overlap_files, files_over_limit) is re-raised every ~10 min after a not-now (r207-r215 on 11-01 04:35-05:55Z, unchanged facts). A not-now should hold until the facts change.
- #1444 (group conflict cut): Jin allowed hand-merge (PM 11-01 07:15Z), only #1444. At merge-lock-wait: check PR CI green, fa orch stop 1444 --keep-work, send PM PR/branch/SHA. After merge: confirm gate reinstall on next FA merge contains it.
- Architect called 07:20Z for 'merge first, revert on break' (Jin via PM): forward result to PM with options; Jin decides.
- Overlap 5 on src/foragent/tracker.py and tests/test_tracker.py for #1444 (07:30Z). Remove (PATH=off) after #1444 merges.
- r224 A (merge-first, opt-in, default off) chosen by Jin: draft via cxdo in chk1415/_drafts/merge-first -> copy to drafts/merge-first, preread, send PM (PM approves via Jin card). Flaky-test order (r225) must land before A is enabled.
- #1007 re-approval plan ready (drafts/1007r/plan.json, preread clean). Publish it batched right after the next FA merge (PM ok'd orchestrator re-approval). #1004 overlap stays (PM).
- Next FA merge, batched publish: drafts/1007r/plan.json + drafts/1170t/plan.json (then retry #1170 with hint). #1447 waits for PM/Jin on tests/conftest.py harness hold (A: add to Files; B: retry without it).
- Next FA merge sequence (11-01, PM): tell PM at once; I publish drafts/1007r, 1170t, 1447h (then retry #1170 and #1447); tell PM when done; PM publishes merge-first. After the gate has #1444+#1424: move merge-wait runs with stop --keep-work -> resolve --verdict pass; try ONE first, confirm it enters the turn without a review, then the rest in small batches; only heads that already passed review+CI. resolve runs in the CLI process until merge: run it in the background.
- GitHub timestamps ran +1h from ~08:00Z on 2026-11-01 (mergedAt, comment createdAt; API Date header correct). Guess, unverified: US DST fold 08:00-09:00Z. Re-measure after 09:00Z and tell PM. Defect candidate, low priority: orch_park.py:42 and inspect_data.py:129 order comments by created_at; order by comment id instead. Avoid unpark 09:00-10:00Z of anything parked 08:00-09:00Z.
- Merge-first counterexample (11-01): #1062 head 02444293 CI green; merging main 77938bf1 (path-disjoint except agent-memory) fails tests/test_watch_authored_waits.py 20/21, reproduced; main alone passes. #1180 added lane waiting text, #1062 redrew the lane scene. Architect's 0/333 no longer holds. Options A1/A2 (run moved tests on merged tree, recommended)/A3 sent to PM for Jin. #1049 regression did not reproduce (8 pass before/after).
- Jin chose A2 for merge-first (11-01): cxdo fix running in chk1415; then copy to drafts/merge-first, preread, send PM with measured check time and recomputed effect. PM publishes after next FA merge. Waiting on ssot-race root cause in mainchk (test_ssot_hints CI failures, 8 of 12 finished runs).
- #1422 (S1) stopped with work kept 11-01 ~08:55Z: retry only after the test_ssot_hints fix merges (PM).
- 11-01 ~09:15Z: #1459 = fix for test_ssot_hints product race (top priority). #1422 retry only after #1459 merges. Move test on #1343 running. To order: at_capacity combination explosion; read comparison lost when CI ends instead of at the merge turn (9 of 14 waiters carried lost for hours while PR CI shows 9/9). Report lost numbers with any hand-merge handoff.
- resolve path limit (11-01): a real textual conflict with main (#1206, tests/test_order.py) stops resolve with 'PR base changed after verification' risk-high, as the normal path does; send it through retry. Check with git merge-tree before moving a run.
- Defect to order: a run waiting in await_turn never checks that its issue closed or its PR merged, so a hand-merged run stays as a ghost holding a slot, file claims and a queue place (#1424, #1444, 11-01). After a hand merge, stop the run right away.
- 11-01 ~10:20Z Jin via PM: add no new merge-path mechanisms. STOPPED: merge-first publish, S1 #1422 (stays stopped), #1142, group-size work. group-reuse fix: draft only, do not publish until the architect answers. CONTINUE: #1459, #1447, overlap-cost fix, early comparison read, running work, team mode. Architect called 'merge path from scratch': forward its result to PM.
- r244 with Jin via PM. Publish overlap-cost and early-comparison (drafts/) batched right after the next FA merge if preread is clean. group-reuse and merge-first wait for Jin's answer.
- Jin: A + keep groups (merge_group_max fixed 4; one path for 1..N). Architect called for cost and issue split; group-reuse draft stays unpublished, drafts/merge-first and #1142 stay stopped. PM makes the card from the result.
- Shard-red proposal (PM 11-01): sent A/B/C to PM. early-comparison publish is on hold until the answer. Red shard needs: fail step after upload, absent-safe output, tracker reports accept completed-with-artifacts for group path.
- Jin chose shard-red A (11-01). Draft in mainchk/_drafts/shard-red -> copy to drafts/shard-red, preread, send PM (PM cards and publishes; align order with the architect's merge-path card). early-comparison abandoned.
- r247 (A + groups: cost and issue order I1..I8, question on order commits) forwarded to PM. Overlap-cost publish held: must follow I3 and drop plan-limit cases. Unverified risk: removing a passing locked test in a PR may count as lost; check before S2/I4d.
- Jin chose order-commit A: delete neutral moves; gather order commits through D85 batched publication (#1264 done, #1312 open). Keeping groups still being discussed with Jin; PM sends the final order.
- Final order, Jin via PM 11-01 ~11:00Z: settings merge_group_max: 1 + overlap_mode: fixed (wait for PM's go after my code-path facts), then I1 test -> I2 shard red -> I3 one overlap limit -> I3b overlap cost -> I4 batch delete S2 (after #1372; check locked test deletion vs lost) -> I5 recheck only Done-when and local_only -> I6 delete neutral moves. I7a-d and turning groups on are deferred; keep group tests running. After I6 measure queued-work failure rate and merge queue time; ask Jin keep or delete groups (bar: failure <10%). I send drafts with preread to PM; PM cards and publishes. #1422 re-look after I2. merge-first and group-reuse stay unpublished.
- PM OK: next FA merge -> commit merge_group_max: 1 + overlap_mode: fixed batched with any publishes; ff the main checkout. Parent-sync starvation (41 parents, 10 s budget, restarts from lowest): draft parent-sync-starve in scratchpad chk3; publish+approve myself if preread clean. Closes #1402/#1414/#1443 by itself.
- Jin via PM 11-01 11:10Z: order now measurement records + I1 first, then I2. flow-records draft running in chk3. Deleting a locked test in a PR was run: counted lost, and the run holds acceptance-touched first; delete locks on main first (policy undecided; S2 needs it). shard-red: lost IDs step being added in mainchk. parent-sync-starve and merge-path-pin in drafts/, preread running.
- Next FA merge: tell PM at once. My publishes first (parent-sync-starve if preread clean), then tell PM done; PM publishes its unapproved batch. Measurement and I1 go to PM for cards, not published by me.
- #1049: review holds 11-01 09:06 (premise=None) and 11:44 (check-then-act across clones). Next hold of this kind: redesign the issue, no more hints.
- Defect to order (choice-free): pr_failed_logs returns empty while a failed job's workflow run is still in progress (it filters on run conclusion; pr_checks fails on the first red job), so the worker gets 'CI failed:' with nothing and burns retries blind. Seen on #1447 PR 1479 (test (1) failed, other jobs pending), #1049 PR 1277 and #1422 PR 1441. When retrying these, paste the failing test names into the hint.
- Order: measurement+I1 -> ci-empty-logs (my fix, draft in chk3, I publish after preread) -> I2.
- Jin approved flow-records, merge-path-pin, shard-red cards; do not touch those three drafts. Next FA merge: my publishes first (ci-empty-logs if ready), tell PM; PM publishes measure+I1 approved, I2 unapproved (PM approves I2 after ci-empty-logs merges). S5 = A: Codex editing specs-delete-v3, then preread and send path. S2/S5 parameter node comparison in that run.
- ci-empty-logs in drafts/, preread running: publish+approve myself right after next FA merge (29 empty CI retries in 7 days). S5 v3 preread running; S2 v2 re-preread running.
- S2's four batch-only parameter nodes may go (PM). When ci-empty-logs merges, tell PM (PM approves I2 then). S2 after #1372, S5 after #1312 and S2.
- #1296 parked 11-01 ~13:00Z: reviewer found the same class twice (base measurement can import head's installed code). Options sent to PM.
- #1296: B chosen, parked, comment posted; PR #1519 stays open. After flow records: count runs blocked by base failures, decide with PM.
- #1208 parked, do before public launch (Jin): r115 small scope (retry only reads and checkable one-time writes; else one clear line and stop with work kept). Order: after the merge path cleanup (same files). Never close it from local records alone.
- MERGE_WAIT_SECONDS 3600 is cumulative per run (run_merge_turn.py:38): #1062 and #993 were cut. Runner request says resume review; check lost before choosing resolve vs retry.
- Acceptance CI job has no timeout: #1447's hung test held shard 3 until I cancelled it (20+ min, default limit is 6 h). Add timeout-minutes with I2 or a small fix.
- Until #1312 merges (PM 14:10Z): publish everything in ONE batch right after a merge (#1511 edit, merge-wait-limit, #1372 edit, PM's); not-ready goes to next batch; exception only if main is broken. Tell each other when done. Why: 8 of 9 main moves in 1h38 were our own commits.
- #1186 parked by PM (option C), PR #1516 stays open: after the merge path cleanup, bring A/B back to Jin via PM (comment on issue).
- Wait limit: go with 16 h now; deleting it is decided when a merge owner lands (PM; architect said delete).
- Published 11-01: #1523 collect check, #1534 wait limit (16 h), #1372 redesigned. #1105 and S2 follow #1372's merge.
- #1447 merged 11-01 16:15Z (slots released while waiting). Measure: did real waste fall? Shard 3 took 3 min on its last CI, so the 5400 s stall is gone there.
- #1525: if held again in the same class, PM splits the draft in three: (1) append-only store with size limit plus tick and priority records, merge first (2) label and blocked_by history (3) reasons for not starting. Do not hint a third time.
- #1511 (I2) worker raised QUESTION: test_ci_empty_logs vs issue. I own that test (#1507). Correcting via scratchpad cel (cxdo); then add to #1511's edit with PM.
- Start order (overlap count before global priority, #603): keep for now (PM B). When flow records exist, compare time a priority issue was passed over with conflict cost, then decide.
- Prompt defect (PM, verified): worker and review prompts omit checklist and unknown sections, preread shows them. Order drafting in scratchpad pwhole; publish+approve first in next batch, put first in queue. #994's two lost lines must go in Out of scope at its re-approval, not checklist. Approved open issues may be held on those lines after this.
- Edit lines follow-ups: #1004, #1482 restored 11-01. #1556 published (refuse removals). #997, #1007 restore still due (read drafts first, drop lines later decisions replaced, write the reason; put must-not lines in Goal). #994: compare 2 lost lines with its PR diff.
- A publish whose push is rejected leaves its order commit on local main and blocks later publishes (seen 11-01 with #1004; I removed it with reset --keep origin/main). Candidate defect.
- Candidate defect, cause measured: a run resumes from the LOCAL branch ref refs/heads/foragent/issue-N, not origin. After an order edit changes a lock, a stale local WIP with the old lock fails hash-mismatch (#1482 twice, 11-01). Pushing to origin alone does not help; I set the local ref too. Why #1511 did not hit this is not yet read.
- #1556 lock correction in scratchpad stale (test_publish_refuses_stale_overwrite); after publish also set local ref foragent/issue-1556 with main merged, else hash-mismatch. #1158 re-approval drafts/1158r in preread (park stripped approved).
- #1525, its branches and PR #1557: touch nothing, no retry, no unpark, no ref changes. PM publishes the store work as a new issue number; closing goes to Jin in the morning via PM. Prompt order published as #1570. #1372 edit: preread pre-1372t2.out, next batch; its branch has old work, so check the start ref. #1007 etc. done.
- Order freeze (PM, 11-01 23:10Z): no new orders or issue edits until #1527 merges, except main broken or an edit that frees a blocked issue. Try flush on the first batch of exceptions. New defects go in fa orch note, not orders. Candidates so far: publish leaves a commit on local main after a rejected push; a stale local WIP lock gives hash-mismatch after a lock edit; park strips approved; held text wrong on old tool; 'deferred' reads like failure.
- #1004 next hold (r262 accepted): Goal edit: failed entry read = no action; multi-step PR chains a v1 non-goal; findings outside locks go to NOTE; only after the run stops; allowed under the freeze exception. Same shape for #1158 on one more hold. r261 (review scope after a PASS) is Jin's, with PM.
- After #1527 merges: publish docs order from r264 and skills/fa-docs edit from r265 (or fold into the docs-owner card). r266 (rules.md:34 overlap rule) is Jin's, with PM.
- After #1527 merges: issue to delete the push outbox and its code including the file (Jin: he receives no pushes, 11-02 via PM). Leave notify's default as is until then. #1525 and PR #1557 are still open: PM command was blocked, Jin closes them himself; do not close them or approve #1525.
- Freeze lifted by Jin (11-02 PM msg): fast waste cuts first, measurement continues. Order: 5A review PASS scope (r261, draft in scratchpad r5a, PM reviews card), then 6A rules doc (r266 A), r264 docs, 2A push outbox delete. Use fa order flush for batches. 1B panel on all first reviews only after 5A merges. Report I3 value to PM. Baseline ~/.cache/fsev-watch/baseline-2026-11-02.txt.
- 5A conditions (PM): risk-map files exempt, marked per entry; merged-in main counts as changed; one setting turns it off. Publish right after the next FA merge when ready; do not wait for the review quality scan.
- Review plan (Jin via PM, 11-02): 5A first (PASS scope with 3 conditions, scratchpad r5a), then review-procedure order (plugin's five perspectives + critical path, scorer 80 cut, scratchpad rproc). 1B is absorbed. Show PM draft and tests before publishing. I3 after these and 6A; overlap above 3 after measurement.
- #1040 tests draft in scratchpad sk1040; order after 5A and review procedure; track by number (PM). main lane skills: not a one-line config (see msg to PM 11-02).
- New orders go into the owning lane, not main (PM 11-02). #1040 also carries default skills for no-lane issues. #1372 and #1526 left as they are.
- #1040 split (Jin): first part = default skills reach orchestrator, docs, architect, code lanes, no-lane issues, reviewers (panel and scoring); fa init; catalog; log counting; no skill names in tests. Later: trials, retirement, proposals. Review procedure goes in a skill file, not prompts, and depends on the reviewer skill path. PM supplies the skill list. docx, pdf, pptx, xlsx excluded.
- After the skills delivery issue merges, set defaults (Jin approved via PM, 11-02): workers and no-lane issues: prove-it-works, fix-root-causes, attack-the-premise, sequence-verifiable-units (subtract-before-you-add, redesign-from-first-principles stay catalog). Reviewer: code-review procedure, critical-path perspective, prove-it-works. Orchestrator: prove-it-works, attack-the-premise. Architect: attack-the-premise, redesign-from-first-principles. Docs: nothing new. Keep lanes' existing skills. pstack is a trusted source. Measure prompt size in that issue.
- Merge turn holder waits for a machine slot since #1447 (found 11-02 00:36Z, no merge 44 min). Draft in scratchpad turnslot. Budget fell to 2 at 00:27, cause not known.
- 11-02 00:39Z paused my drafts for load (PM): stopped Codex for review procedure (scratchpad rproc, fix text rproc-fix.txt) and #1040 (sk1040, sk1040-fix.txt); resume with cxdo fix after turnslot publishes. Budget fell 10 to 5 at 23:54 and 5 to 2 at 00:27 at load 1.56 (halves above 1.5). Not to attribute without measuring; system 51% goes on the after-measurement list.
- Skill layers (Jin 11-02): 1 fa init installs role skills and the approved default table (in #1040 first part); 2 fa init proposes lanes and conditional skills, person confirms (small next issue); 3 rest of #1040. After the merge deadlock; no hurry.
- Retrospectives: fa orch note --retrospective since #999 merged (11-02). The scratchpad retro draft is dropped. #1158 edit needs its preread re-run (pre-1158s2.out), publish when clean.
- #1004 if held again: split, come to PM (not a Goal edit). 5A report must state safety exception is only the two marked risk entries until review procedure lands. Turn fix: 600 s exits must be visible in measurement.
- #1158 retry after publish: hint to restart from a472cbc with main merged (r271). Dream PRs #1551, #1564 per references/dreams.md.
- #1004 retry after publish: hint to restart from PR #1401's head with main merged, fill only restored lines; no ref deletion.
- Docs order also takes r274 (tidy cycle text after D99 and its second item).
- Batch flushed 11-02 01:35Z: #1597 turn fix, #1599 5A, #1158, #1004 edits, #1602 follow-up unapproved. Next: review procedure after #1040 first part; resume rproc and sk1040 drafts (cxdo fix with rproc-fix.txt, sk1040-fix.txt); publish prekey draft; docs order.
- After #1597 merges: tell PM, then fa settings set max_parallel 4 for half a day (8+ merges); watch the merge turn is not blocked; compare merges per hour, time per issue, review failures, CPU, memory warnings. Token script drafts/subtraction-survey/tokens_merged.py.
- Review procedure order also updates docs/gate.md's review section (r279).
- skill-layer-1: defaults per role (PM); after Codex: copy to drafts/skill-layer-1, QA on clean main, preread, flush, approve.
- #1599: hint to shrink first; if it does not shrink from +804, restart from main (PM). .claude/skills root listing is on the MacBook test list.
- r302 docs items (text in scratchpad r302.txt; copy into the r264 docs order draft, gate.md:74 beside r279 in the review procedure order).
- r305: if #1570's next FAIL is again a marker stuck mid-line, reduce that handling and make it NOTE by Goal edit (0 in 400 bodies, 0 in 405 snapshots). r306: if #1523's next FAIL is again a retained entry with no files, NOTE by Goal edit (orders.jsonl 9 of 9 have files). Do not touch the current round.
- r306 extra: before lowering, read how land treats a dirty checkout; if work can be lost, cancel the proposal. r305 hint: remove _GLUED_MARKERS and its unit tests, re-verify locks (not yet proven to pass).
- r309 docs: gate.md §8 text in drafts/r309-docs.txt goes into the r264 docs order. Rule exception question (D60, merge-turn run) sent to the PM with r266.
- r309 answer A (PM; Jin told in one line): the docs order adds a decision in decisions.md and after rules.md:35 'A run holding the merge turn does not wait for machine admission; its work still counts. (Dnn)'. Same order as 6A, r264, r302 items, r309 gate.md.
- #1004 split published and retried 07:03Z. Held again: tell the PM at once; with no basis to redraw, propose B (park). If it does not shrink, restart from #1401's PASS head (PM ok). #1629 is the unapproved follow-up: after #1004 merges, write locks from merged code, re-preread (its first preread question: invalid committed file with no team trace, recommend A, keep today's warning for a single user), approve.
- max_parallel 4 experiment 11-02 07:03Z..18:34:39Z, ended by unset (PM: merges dropped). 11-03 ~09:00Z: #1527 24h split, unknown share (fix first if still high), then #1635 Done when + preread.
- size signal order (Jin approved through PM, 11-02): draft in scratchpad sizegate; show the PM a card, then preread, flush, approve. No automatic redesign, no retry instruction change.
- Experiment measure (architect r313): read review results as ratios, not counts (FAIL after PASS 50% to 25%, rejections per run, 4th+ FAIL before first PASS 140/509). #1597's effect cannot be separated; read with the experiment. Check that the next PASS event carries head and body_sha (r261).
- #1469 edit (Jin: gate calls architect directly; re-call when a number worsens after an answer): draft in scratchpad w1469; publish after the review procedure order, approve. Signals: an open (unanswered) one does not re-raise (orch_role_raise.py:163-206), so lane_concentration can be left open.
- #1523: std3 published 07:18Z, PR +1,160. Held again on a new class: consider split or redesign (D106), not another sentence.
- #1635 (self-sizing slots by machine and work weight; published unapproved by the PM, Jin's decision): write locks from #1527's per-step weight data, then approve. Order after review procedure, #1469, size signal.
- r315 docs: decision for #1599's review standard (after a PASS with the same body, block only on code changed since PASS or a failing locked test; body edit restores full review; exempt risk entries keep full scope; review_pass_scope false restores old) + one rules.md line under Review; text in drafts/r315-docs.txt. Docs order.
- Role memory committed b0d551fa (2026-11-02); defect order draft in scratchpad rolemem still to QA/publish.
- UX pass 1: draft in scratchpad uxfix. Publish only README (#1477 edit) and help/doctor small issue. The fa init setup-help part (A, with #1475) stays a draft: Jin is choosing the entry point (/fa conversation vs fa init); the PM will send the scope again.
- #1499 #1500 #1501 (decisions not buried): must-have before launch; order after review procedure and #1469. Tell the PM when their turn to write locks and approve comes.
- Launch bundles (Jin, 11-02): 1 speed fixes (#1526 #1527 #1644 S2 S5), 2 review quality (review procedure, #1619 and table, size signal), 3 self-watch (#1469, #1499-#1501, role memory commit, promise list), 4 first impression (#1475, /fa first run, #1477, terms, help/doctor, #1208, #1491, #1492, #1160, #1489, #1490), 5 UX researcher and MacBook dogfooding (#1503). After launch: merge owner, team mode (#1004 #1007 #1008), rest of PM single entry, subtraction.
- #1005 question: architect call refused (machine budget 4/4 full); asked via cxdo scan (scratchpad q1005.out). Possible: bundle 1; blocked: after launch. Relay short to the PM.
- Cost seen 11-02: my lone #1619 order commit (08:08) moved main, restarting #1526's CI while it held the merge queue head; #1523 waited behind it. Lone publishes cost a CI round for the queue head; keep them for real emergencies.
- #1005 answer (11-02): POSSIBLE. GitHub merge passes only --match-head-commit (tracker.py:1591); order publish uses foragent-publish.lock, merges use merge.lock; main has no branch protection. Recommended bundle 1 or the smaller fix (publish takes the merge lock). PM decides.
- Draft QA queue, one at a time with nice (PM 11-02): sizegate, w1469 (#1469), rolemem, uxfix (README #1477 edit + help/doctor issue only; init-help part held), then #1499-#1501, then first impression (#1489 #1490 /fa first run #1475 #1477). Send the PM one card for the batch.
- #1469 draft lacks "a role call is not refused or delayed by full lane slots" (architect call refused 08:18Z at 4/4). After mainlock, cxdo fix w1469 to add it with a lock, then QA.
- PM boundary flushed ef419f9c (6 unapproved placeholders). Review procedure draft (rproc) becomes an edit of its issue.
- r320: #1526's r281 review standard stays order-only (no rules line); r315's D109 covers only #1599's review scope.
- #1619 r319 hint applied 09:31Z. Held again on the same class: tell the PM (tonight's test).
1004 PM: 09:39 proposed B; waiting.
- r324 docs (order.md: publication refuses an order whose locked files cannot collect, #1523): drafts/r324-docs.txt, docs order.
- Approval-record defect order: drafts/approval-record, my QA passed (A 6 + B 4 locks fail on main, 90 pass with prototype, +21 -9); preread, publish+approve at a merge window.
- Main-write lock is #1662 (approved 10:10Z, skills lane).
- #1640 Done when narrowed (drafts/1640-dw, preread); publish at next merge, then retry.
- MacBook first-user test (11-02): no deadline tonight any more. Jin moved it after bundle 4 (npx install, /fa first run, README, terms, help and doctor), with the UX researcher (#1654) going first. #1619 publishes with the merge batches like any other issue.
- r330 for #1619: round 10 FAILed in class (a) at 12:58Z; apply at its next hold, retry hint (text in drafts/r330.txt): file-tool reads are literal, only single-operand cat/head/tail/sed -n of one SKILL.md counts from shell; the nearest LICENSE walking up to the checkout or project root alone decides and is copied, a same-name library skill never substitutes; other shell forms and licenses above are NOTE.
- r334 docs: #1619's license rule becomes a decision + rules.md line (A, already Jin's). Docs order.
- r338 docs (gate.md:129 review_pass_scope comment, rules.md line for D105 보강): drafts/r338-docs.txt, docs order.
- Budget after unset stays 4 and climbs 1/h (was 8): goes into #1635 Done when as "removing or raising the cap restores the pre-cap budget on the next tick" (comment on #1635). Mark 18:34Z..~22:30Z in the 11-03 measurement.
- r347 docs: r334 decision text must use #1652's final license scope (drafts/r347-docs.txt).
- batch-2026-11-02o flushed faf8e59e 22:32Z: #1696 size signal, #1469 edit, #1699 role memory (approved). Size thresholds: check wrong-estimate share ~11-10. Next card: #1651 review procedure (scratchpad rproc), then UX (uxfix).
- b4 approved f6620d4f: #1208 #1160 #1484 #1491 #1492. Pending: #1489 and #1490 lock rewrites (Codex), help-doctor preread, README (#1477) waits Jin via PM.
- r352 conditions (text drafts/r352.txt): #1696 if round 5 FAILs on estimate binding → store expected_size on the run record at run start, merge reads only that. #1469 if round 5 FAILs on waiting_on_person → remove that alarm from #1469 (Goal edit at retry boundary) and order it separately with its own lock.
- S5 (drafts/specs-delete-v4, Jin-approved 11-01, never published): PM asks me to rebuild on current main like S2, verify, preread, approve directly; order after #1651 → #1650 → #1489/#1490. Rebuild on current main like S2, after #1651 → #1650 → #1489/#1490. PM writes #1655 and #1475 tests. README B edit (drafts/readme-b.json + readme-b-prototype.patch) prereading; C unapproved in same plan.
- Jin 11-03 (via PM): lane agents (fa-<lane>) draft Done when tests for placeholder issues in a separate session; orchestrator only checks red-on-main for a real reason, then preread and approve. Until the process issue merges, do it by hand: #1650 → fa-view agent (scratchpad outbox), process issue → fa-order agent (scratchpad testdraft); #1655 and #1475 → adopt lane. Codex drafts stop for these.
- #1651: preread only; approve only after the architect's FAIL classification (requested 00:05Z), Goal may change (PM 11-03). #1489/#1490: preread and publish.
- #1490: init-fa-allow already merged (#1232, PR #1432). Fix its checklist line, approve per PM; Jin must accept Bash(fa:*) once (fa init --fa-allow yes) before #1490 merges — permission setting, not mine to change. Asked PM A/B.
- Rework part in #1527 split (PM 11-03, bug fix, orch s): fa-orch agent drafting in scratchpad rework; approve after clean preread. 09:00Z measurement shows rework separately (PM: 651 of 738 unknown minutes are rework).
- Lane drafts running for the 3-part process (11-03): order (red check, done: drafts/lane-test-drafts), orch (draft sessions, scratchpad testdraft2), roles (wait facts, testdraft3). Then one card to the PM. README C split into drafts/readme-c.json (publish unapproved). fa init commit 3441f14c (Jin) moved main: watch for PR re-runs.
- #1655 (/fa first run): adopt agent drafting in scratchpad firstrun with Jin's 11-03 consent screen (5 items, each can be turned off, nothing changes before consent, no terminal: print next command and stop; lock: rerun without consent makes 0 commits and pushes).
- Person-only fa commands refused inside FA-launched sessions (PM 11-03, bundle 4 bug fix): fa-view agent drafting in scratchpad persononly; approve after clean preread. #1469 redesign (one baseline, drop person-wait alarm) prereading drafts/1469-std; then retry.
- Person-wait alarm moved out of #1469 → new issue (bundle 3, parent #1469): fa-roles agent drafting in scratchpad personwait; approve after clean preread.
- Collect-check vs test slots: issue drafted by fa-order agent (scratchpad collectslot), bundle 1, approve after clean preread. The two FA idea notes (371952c0, b627d402) cannot be removed: --replaces works only for decisions; they stay; at the after-flow-measure flush they land in ideas.md and the docs role links or removes them (PM: no note-delete issue).
- Collect-slot fix draft (drafts/collect-slot, order s): new locks red on main for the slot timeout, prototype passes. Preread with retries; publish first (it unblocks every other publish).
- PAUSED all lanes at 00:48Z until 2026-11-03T02:18:37+00:00 (PM: free a test slot to publish collect-slot and the blocked batch, then resume at once).
- Person-only guard draft (drafts/person-only-commands, view s): 47/50 red on main, 50 pass with prototype; orchestrator kept allowed (resolve, dream merge, settings) — PM to confirm; preread after the paused-window batch.
- Person-wait alarm draft (drafts/person-wait-alarm, roles s, split from #1469): 8 red on main, 8 pass with prototype; approve after clean preread.
- #1655 first-run draft (drafts/first-run): 10 red on main/pass with prototype; changes 16 locked files (--yes); PM asked about item 1 in new projects (#1232 rule) and skill text; preread after.
- #435 split (drafts/435split): unit 1 silence-checks proven (19 cases pass with prototype) -> approve; unit 2 silence-baseline-resume and #435 edit (13 cases) not prototyped -> publish unapproved, prototype them before approval (orch agent, when load allows).
- #435 split publish refused: its watchdog lock fix touches tests/acceptance/test_silence_watchdog.py, which approved+running #1641 also carries. Publish right after #1641 merges (drop batch_branch=None from the lock diff then), preread again, approve-keys silence-checks.
- Docs order must carry #1477's old scope: comparison docs match launch behaviour; old plans/notes outside docs/specs and docs/impl removed or folded.
- #1655 consent child (drafts/first-run) publish refused: its --yes change to tests/acceptance/test_cli_split.py collides with approved #1725 and #1729. Publish right after both merge (re-check test_cli_split diff on new main), preread, --approve-keys first-run-consent.
