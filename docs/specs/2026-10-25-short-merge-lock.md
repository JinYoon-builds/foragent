# Merge lock covers only the final merge step

Evidence (2026-10-25): run_publish.py `_publish_locked` holds merge.lock through premerge checks and the whole CI wait; #248 held it while polling PR #398 CI and #386 waited 18 minutes behind it. Jin decided to shorten it now (D88, waiting).

- Premerge checks and the CI wait run outside merge.lock; the lock covers only re-checking the base and merging.
- If main moved since the checks, reuse #376's classification; otherwise release the lock, re-check outside it and queue again.
- Never merge a head whose CI is not green.
- Each merge records time from review PASS to merge and time holding the lock.
