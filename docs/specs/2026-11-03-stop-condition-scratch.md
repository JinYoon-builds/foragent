# Stop condition: scratch work is not an outside edit, judged on the end state

## Problem (measured 2026-11-03)
The lane run's stop condition (the executor goal) says "you did not edit files outside this
repository. Files that test tooling writes outside the repository do not count as your edits."
In two days 35 lane runs hit the stop-hook block cap (9 blocked turns, then the harness ends the
turn), all because of this clause. Example: runs/issue-1489/issue-1489-executor-9613.log. The run
did a manual CLI trial in `D=$(mktemp -d)` with `HOME=$D/home`, then `rm -rf $D`. Every later stop
check said the clause was violated and "deleting it afterwards does not undo those writes", so the
condition could never become true again.

## Wanted result
- Scratch work in a temporary directory the run created (the system temp directory or a run
  scratch directory) is not an edit outside the repository, deleted or not.
- The clause still forbids changing the person's real files outside the repository, named by
  example (for example ~/.claude, other repositories, launchd).
- The condition is judged on the final state of the run, not on an earlier transcript step: an
  earlier failed command or an earlier scratch write that the end state no longer shows does not
  keep it false.
- Unchanged: Done when commands pass from the repository root, the acceptance directory is
  unmodified, test-tooling writes do not count, the 3500-character cap truncates only the
  acceptance path, and 40 turns end the run without repeating the stop check.

## Acceptance
`tests/acceptance/test_stop_condition_scratch.py` reads the public goal text and checks the three
wanted results for a default, a custom and an oversized acceptance directory.
`tests/acceptance/test_stop_condition_provable.py` stays as it is and must keep passing.

## Notes for the lane
- The exact goal wording is also pinned in tests/test_run_prompts.py
  (`test_native_goal_condition_length_cap_preserves_instructions`) and quoted in
  docs/impl/p7c-native.md. Update the asserted text and the quote with the wording; keep every
  test name.
- A scratch prototype (`prototype.patch`) proved the locks reachable; the wording is the lane's
  choice.
