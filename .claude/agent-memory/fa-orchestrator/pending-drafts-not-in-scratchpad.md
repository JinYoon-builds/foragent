---
name: pending-drafts-not-in-scratchpad
description: Test rewrites and plan.json drafts waiting for a later approval must not live only in a session scratchpad; they vanish
metadata:
  type: feedback
---

Keep any draft that waits for a later trigger (rewritten locked test, plan.json for "approve after #N merges") in a durable place, e.g. `~/.local/share/foragent/JinYoon-builds__foragent/drafts/<issue>/`, not only in the session scratchpad.

**Why:** 2026-10-30 the #174 rewritten test saved in an earlier session's `scratchpad/t174/` was gone when #154 merged, so the work had to be redone (Codex scan). Scratchpads are per session and get cleared.

**How to apply:** when a memory note says "draft ready in X, approve after Y", X must be a durable path. Re-approval flow that worked: copy the previous plan.json, change id/title/spec, append a "Test correction" sentence to the Goal, put the edited test in `edits[].acceptance_files`, `git checkout` the file on main, then `fa order preread PLAN` → `fa order publish --approve PLAN` → `fa orch retry N --hint`.

Pitfall seen twice on 2026-10-30: specs or Goal text written through an unquoted heredoc (`<<EOF`) run any `backticked` command and drop it from the text. Use `<<'EOF'` or write the file with the Write tool.

Preread false 'unknowns' (2026-10-30/31): plain-text plan lines from the preread worker count as unknowns and block approval. Workaround until the planning session's fix (after #1014): append to the Goal 'Preread answers use only DECIDE: lines for your design choices and QUESTION: lines for real unknowns; no other text.' then preread again.

When correcting a locked test for a behaviour change, grep every locked test that observes the same output first (e.g. `grep -rln '"skills", "catalog"' tests/acceptance`) and fix all in one edit; on #1028 I fixed 3 of 4 and cost a run.

Lesson (2026-10-31): when a locked or unit test fails on main, bisect locally AND compare with GitHub CI before calling it host-specific. Two main-wide failures today were time bombs (fixture clock vs real clock: test_cli notify at UTC 08-09, test_presence after fixture+24h), first misread as machine-specific.

Lesson (2026-10-31): `fa orch resolve N` on an issue whose run is still alive (e.g. merge-lock-wait) blocks until that run ends; the #1191 resolve at 14:30Z returned only at 20:30Z when the run timed out. Resolve only blocked issues with no live run, or run it in the background and do not wait on it.

Lesson (2026-11-01): switching team review to batch made runs target foragent/batch-* branches, and ci_mode.enabled() is true only for the base branch, so every batch-target run silently fell back to the full local protected suite (~1600 tests) and hit the 900 s limit. Before changing review mode, check what it does to run targets and CI mode (fix ordered as #1369).

Lesson (2026-11-01): find an issue's PR by its head branch (`gh pr list --head foragent/issue-N`), never by searching "Closes #N in:body". The body search returned #1188's PR #1399 for #1141 and I sent the PM the wrong PR to hand-merge. Always send the head branch with the PR number.

- Before a plan names an existing locked test in done_when, run that file on main. 2026-11-01 #1415 blocked: test_merge_group.py (stale r17 contract, #973) was 13/18 red on main. Applies to my own drafts too (S2/S5 edit 22 locked files).

- Each `gate run-issue` process runs from its own pinned dir under ~/.local/share/foragent/tool-versions/<time>-<sha>-*; a reinstall changes only NEW runs. Check with `ps -ax | grep "gate run-issue"`. Merge groups are formed by the turn holder and recorded as orch-events.jsonl kind "group-check" (merge-queue.jsonl turns always list one member). 2026-11-01: after #1403/#1415, 21 of 22 waiters were old code, so no groups formed.

- When I change a locked test (order edit), grep pending drafts for the same file (`grep -l <file> ~/.local/share/foragent/JinYoon-builds__foragent/drafts/*/plan.json`) and tell the PM which waiting drafts (S1/S2/S5 etc.) carry a stale copy. 2026-11-01: S5's copy of test_d85_flush_recovery.py would have reverted the #1312 --force fix.
- Draft lesson: a new workflow step that calls a new module from the trusted (main) install fails on the PR that introduces it (#1422 CI 11-01). Specs adding CI steps must say: degrade to today's behaviour when the trusted tool lacks the feature.

- 2026-11-01 07:06-07:55Z: no FA merge for 50 min because main moved 4 times (3 of my order publishes + 1 hand merge) while #1170 held the merge turn; each move forced refresh + re-verify + CI (~12 min). In CI mode every main commit costs the turn holder a full round. Batch order publishes (one commit) and publish right after a merge, not one per draft. See also [[batch-main-commits]] in the user memory.
- A gate tick ran 14+ min at ~90% CPU on 11-01 07:38Z; during it `fa orch stop` returned "gate가 바빠요" every time. Measure tick duration before blaming other locks.

- Raising an overlap limit is expensive until fixed: overlap_files.at_capacity enumerates combinations(peers, size) x file products per waiting issue. With limit 6 and ~20 peers, watch --once took >100 s; removing the raises: 7.8 s (11-01 09:0xZ, measured). Suspected same cause for 14-min gate ticks, not yet measured. Do not raise above overlap_per_file until the fix merges.

- `fa orch queue --global N ...` replaces the whole list. My one-liner that read the old list from --json parsed nothing, so several updates on 11-01 silently wiped the list (team-mode order was lost until 10:00Z). Read ~/.local/share/foragent/JinYoon-builds__foragent/global-queue.json and check the printed list after every change.

- A locked test may import only files on main at publish time. merge-path-pin's locked test imported the unlocked tests/merge_path_expected_red.py, which publish does not write, so `collect` failed on main and all PRs from 12:32Z to about 13:00Z on 11-01. Before publishing, run pytest --collect-only for new acceptance files on a clean main checkout with only acceptance_files applied. I passed this draft to the PM without doing that.
