# Neutral base moves preserve the verified PR head

## Outcome

When main moves only through paths the current run can prove result-neutral, keep the verified PR head unchanged. Do not merge that base into the PR or publish a replacement head. Reuse the passed machine checks and CI evidence for that exact head. A non-waiting CI read during the final check still applies; reusing evidence does not waive changed CI, head, approval, acceptance, policy or budget checks.

Role memory under `.claude/agent-memory/` is result-neutral on the same terms as `docs/`. Keep the existing protections for paths this PR changed, mixed source moves, per-commit changes, acceptance ownership and the baseline the run actually checked. Keep existing neutral configuration and other open issues' unverified locked-file behavior.

Source moves follow today's refresh, re-verification and new-head CI path. A source move after one or several neutral moves must still be detected and checked. Main keeps both its neutral changes and the PR result when GitHub squashes the unchanged head onto the current base.

The final check, merge and completion retain the existing final merge lock and #1662 shared main-write lock boundary. A neutral move observed during lock acquisition releases that attempt and receives a fresh final check before merging. Slow verification and CI waits stay outside that lock. No main write occurs if a final prerequisite no longer holds.

## Evidence and cause

The supplied measurement dated 2026-11-02 says the run logged “base moved: only result-neutral paths; verification reused” but merged main into the PR and published a new head. PR #1523 then spent 17 minutes in CI after the prior head had already passed, delaying the merge queue. This order does not claim a fresh live GitHub measurement.

On this main, neutral classification skips local verification but the publication flow still integrates the base and resets publication/CI state. CI comparison evidence is pinned to its measured base and rejects a different base. Role memory is absent from the documentation directory exception. These are inconsistent evidence/publication mechanics and a missing declared neutral path, not missing agent judgment. Reuse the existing classification and final-check mechanisms; add no semantic heuristic, agent step order or scripted LLM judgment.

## One issue

Key `neutral-move`; size `s`; interactive `false`; blocked by nothing in this order. Expected implementation areas are `src/foragent/run_base_move.py`, `src/foragent/run_publish_flow.py` and `src/foragent/ci_mode.py`. These files match no explicit lane path in `.foragent.yml`, so use the implicit `main` lane. No lane configuration or fixed skill change is requested. The lane chooses its implementation and useful per-issue skills.

The order owns `tests/acceptance/test_neutral_move_order.py`. It also updates the existing base-move and short-lock acceptance fixtures where their branch-ancestry assumptions or fake main overwrite encode the old behavior. Each replacement starts from current main and retains every existing parametrized node ID and lock assertion. The replacements preserve the result and source-move checks while accommodating unchanged PR heads. The unchanged #1662 all-writer serialization acceptance file is an additional Done when command. Full replacement contents are embedded in `plan.json`. The ordinary tests in `tests/test_ci_mode_unit.py` and `tests/test_short_merge_lock_unit.py` need immutable per-head report bases and unchanged-head expectations for neutral implementation moves. CI-reset and new-head timeout controls use source moves. Review-only behavior remains as it is.

## Observable acceptance

Use the real public runner with local Git histories and fake models/GitHub. Inspect published heads, approved command executions, immutable CI reports, waiting, final-check refusals and the resulting main contents. Do not pin private helpers or implementation names.

- For docs, role memory and another approved open issue's excluded locked file, move main before publication, after CI passes and at final-lock acquisition. The verified result and original published head survive, no new CI round/wait starts, and main retains both results.
- After neutral reuse, a source move still creates one refreshed head, runs the approved checks again and waits for that head's CI before landing.
- Revoke approval, fail CI or replace the PR head at final-lock acquisition after neutral reuse. The run must refuse the merge and leave main untouched.
- Existing related base-move and merge locks remain the regression bar, with only the explicitly supplied obsolete expectations replaced.

## Interaction and scope

The all-main-writer serialization from #1662 is already on this main. This order must work alone on that main, keeping the shared main-write lock from final prerequisite checks through merge completion. It must not duplicate or replace that lock contract. Re-prove both `tests/acceptance/test_main_write_serialization.py` unchanged and every current-main node in `test_short_merge_lock.py`, together with this draft's locks. `REPORT.md` records this combined proof and any remaining limits.

No review-only entry redesign, group-merge redesign, new approval boundary, external publication, CI configuration change or broad test-suite cleanup is included. No stronger cross-machine atomicity guarantee than today's merge path is claimed. The scratch prototype is evidence, not a prescribed implementation.
