# Flag-off source audit

Historical prototype document; see the [current-main port report](merge-owner-port-report.md)
for the port's compatibility decisions and verification.

Generated source-hunk inventory against pre-prototype commit `232cd955`.
Line spans refer to the current working tree, including the earlier prototype.
Each row covers every added/modified source region in that file; removed lines have no current execution path.

Two compatibility cases are explicit: operational paths are gated by configuration;
history readers recognize an opt-in `owner_merge` record even after disabling the feature.
For histories without that record their completion result is the original outcome test.
Additive signatures/default arguments are listed rather than being hidden as “owner-only.”

| Source | All current changed line spans | Flag-off disposition |
| --- | --- | --- |
| `src/foragent/bench.py` | 48–49, 52–53 | Accounting-only record guard: run_merged returns the original outcome result without owner_merge; reads add historical owner completions only. No publication or scheduling action is added. |
| `src/foragent/ci_mode.py` | 13, 53, 57, 88, 102–104, 310–311, 322, 357–358, 370–373 | strict defaults false; wait defaults true. Non-rate-limit propagation and terminal owner evidence are guarded by merge_owner/non-waiting owner use. Original waiting fallback remains. |
| `src/foragent/config.py` | 97 | New field defaults false; existing setting defaults unchanged. |
| `src/foragent/config_validation.py` | 89–90 | Only validates the new explicit setting; old mappings retain the same validation. |
| `src/foragent/inspect_actions.py` | 58–65 | Central close boundary guarded by merge_owner; original close in else branch. |
| `src/foragent/merge_owner.py` | 1–514 | Entire module is reached only through opt-in start/handoff/repair; no flag-off scheduling or cleanup. |
| `src/foragent/merge_owner_reads.py` | 1–74 | Entire module is instantiated only by the opt-in owner; no shared adapter caching. |
| `src/foragent/orch.py` | 6, 185–186, 195–204, 206, 529–540 | Authorized handoff and central repair tool guarded by merge_owner; flag-off retains branch lookup, retry marker/close/labels and direct merge. |
| `src/foragent/orch_dream.py` | 176–178, 252–260 | Owner facts and central close guarded by merge_owner; legacy dream count and gh close arguments retained. |
| `src/foragent/orch_facts_runs.py` | 243, 245 | Accounting-only record guard: run_merged returns the original outcome result without owner_merge; reads add historical owner completions only. No publication or scheduling action is added. |
| `src/foragent/orch_reinstall.py` | 62–63 | New target restriction guarded by merge_owner. |
| `src/foragent/orch_roles.py` | 61, 65 | Accounting-only record guard: run_merged returns the original outcome result without owner_merge; reads add historical owner completions only. No publication or scheduling action is added. |
| `src/foragent/orch_scorecard.py` | 53, 64 | Accounting-only record guard: run_merged returns the original outcome result without owner_merge; reads add historical owner completions only. No publication or scheduling action is added. |
| `src/foragent/run_base_move.py` | 180–181 | Additional completion facts read only with merge_owner. |
| `src/foragent/run_baseline.py` | 175–176 | Additional completion facts read only with merge_owner. |
| `src/foragent/run_group_ci.py` | 68–73, 78 | Central close and branch preservation guarded by merge_owner; legacy temporary PR/ref cleanup retained. |
| `src/foragent/run_merge_turn.py` | 15–18, 114, 118 | Original _live restored. Added **fields is empty for legacy callers, producing identical ready rows. Other changes are documentation. |
| `src/foragent/run_publish_flow.py` | 28, 182–192 | Handoff branch guarded by merge_owner; original tail and withdrawal execute with flag off. |
| `src/foragent/run_publish_merge.py` | 37–38, 81–82, 89–92 | Exact-number PR/closed-issue reads guarded by merge_owner; flag-off uses original list_prs. Optional authorization defaults None, preserving original policy. |
| `src/foragent/run_publish_reviewed.py` | 26–27, 48–49, 54–55, 80, 106–107, 109–110, 131–132, 173–188 | Exact PR reads, policy authorization and enqueue are guarded by merge_owner. Optional refresh callbacks default None and are supplied only by the owner. Legacy publication tail retained. |
| `src/foragent/run_review.py` | 547–550, 556–558 | Explicit requested-PR selection uses authorization only with merge_owner; flag-off retains original branch lookup. |
| `src/foragent/run_waits.py` | 40–41, 237 | Independent runtime budget guarded by merge_owner; original worker budget retained. |
| `src/foragent/runner.py` | 151–152, 158–165, 319, 396, 411, 465–466, 472 | New context defaults are None. _handed_off returns False before importing owner code when flag off; all legacy cleanup effects retain their predicates for legacy outcomes. |
| `src/foragent/scheduler.py` | 99–102, 198–203 | Process start and pending-publication board handling guarded by merge_owner. Removed unconditional reaper and synchronous owner call. |
| `src/foragent/scheduler_checks.py` | 69 | New completion predicate guarded by merge_owner; original outcome predicate with flag off. |
| `src/foragent/scheduler_recovery.py` | 14, 148, 262–274 | Admission crash path and completion predicate guarded by merge_owner; legacy recovery branch retained. |
| `src/foragent/state_refresh.py` | 88, 90 | Accounting-only record guard: run_merged returns the original outcome result without owner_merge; reads add historical owner completions only. No publication or scheduling action is added. |
| `src/foragent/state_runs.py` | 173–205, 320–321 | Original record_merge restored. record_owner_merge is owner-only; extra history interpretation is guarded by presence of the owner_merge record. Legacy records retain original time projection. |
| `src/foragent/tidy.py` | 23–24 | Accounting-only record guard: run_merged returns the original outcome result without owner_merge; reads add historical owner completions only. No publication or scheduling action is added. |
| `src/foragent/tracker.py` | 85–86, 115, 117, 127, 173–176, 679–681, 684–685, 687, 689–691, 894, 896–897, 907–912, 947, 1022–1023, 1311–1318, 1367–1368, 1555, 1613, 1619–1620 | Added get_pr/state/head APIs are owner-only call sites. close/merge options default to the same delete behavior and gh argv. Base artifact error remains TrackerError; typed rejection is an owner subclass override. Local merged projection is suppressed only in owner mode pending validated completion. |
| `src/foragent/watch_data.py` | 231, 233 | Accounting-only record guard: run_merged returns the original outcome result without owner_merge; reads add historical owner completions only. No publication or scheduling action is added. |
| `src/foragent/watch_sections/acorns.py` | 39 | Accounting-only record guard: run_merged returns the original outcome result without owner_merge; reads add historical owner completions only. No publication or scheduling action is added. |

This is a source/reachability audit, not a claim that the entire flag-off suite ran.
The final verification runs only the two files requested by the user. In particular,
the legacy 502/default-branch/local-fallback path and legacy PR listing path have a direct regression.
