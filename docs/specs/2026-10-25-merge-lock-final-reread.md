# The final merge lock re-reads every decision input (edit to #437)

A Codex adversarial review reproduced two TOCTOU defects: a CI rerun on the same head that fails while the run waits for merge.lock still merged, and risk:high added during the wait was ignored. Inside the final lock the run now re-reads every merge-decision input, including one non-waiting CI status read of the exact head. The locked rule allows one CI read per hold and still forbids polling, sleeping and verification under the lock. Two new cases cover the rerun failure and the late risk:high label.
