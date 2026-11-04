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
