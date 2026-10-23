---
name: project-221-tick-budget
description: #221 tick GitHub budget — refresh change detection, orch_reads cache, board dedupe, and the locked fixture conflict
metadata:
  type: project
---

- Change detection is a fact comparison: `tracker.listing_changed()` compares the listing's title/body/labels/state/`updated_at` with the row's `remote_updated_at` (GitHub time; the row's `updated_at` is local write time, do not confuse). Refresh skips comments/sub_issues/blocked_by for unchanged issues; a dependent is re-read when any stored blocker changed. Parents store `children` on rows.
- `orch_reads.IssueReads` caches label_events/find_pr/blocked_by in `<state>/issue-reads.json`. An entry is reused only in a tick that itself ran the refresh, when `refresh_sequence` advanced since the read AND row facts (incl. blockers) are equal. Kick ticks without a refresh read live: `test_human_ready_removal_is_not_promoted_again_for_the_same_approval` needs a re-approval seen at once. `_start_ready` stays live before launch.
- FakeTracker `_touch` must reuse the caller's timestamp (tests inject finite clock iterators, e.g. `test_fake_events_are_chronological_and_return_copies`); `add_issue` does not touch.
- Board card id is recorded only after a confirmed value write (`test_board_field_failure_retains_only_confirmed_values`); the stale-card retry applies only to an id that came from the store.
- FakeTracker keeps `updated_at` in `_updated` and fills it only in listings (`_listed`), because tests compare `get_issue()` to `Issue(...)` without updated_at.
- Board: `Project.set_values` skips values equal to the last recorded `board_values` and caches the card id (`board_items`); a failing cached id is cleared and looked up once.
- Pitfall: `tests/acceptance/test_tick_github_budget.py` builds dependents with empty bodies, so the gate flags `no-machine-check` and test 3 cannot pass; asked in `.foragent/QUESTION.md` (recommend fixing the fixture).

**Why:** spec docs/specs/2026-10-23-tick-github-budget.md — gate ticks burned ~2,900 REST calls/hour.
**How to apply:** when adding per-issue GitHub reads to the tick loop, route them through `IssueReads` and add any new row fact they depend on to `orch_reads.FACT_KEYS`.
