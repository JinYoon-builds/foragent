# retry guard (2026-11-07)

Architect r943: with merge_owner on, fa orch retry closes the issue's PR for repair without looking at the merge queue. On 11-06 20:59Z a stale loop retried #2321/#2322/#2323 while they sat green in batch b2bf7363-5; the batch was retired with 0 of 5 landed. Six such retries happened that evening. Both tests are red on main.
