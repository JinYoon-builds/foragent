# Overlap checks stop hammering the state lock (2026-10-26)

A profiled tick took 306 s, 268 s of it waiting on the state lock. overlap_files.measure() wrote the run on every call, update_run scanned the process table under the lock, and the late-file wait re-measured every peer every second. Measurements now write only on change, metadata updates skip process scans, and the late wait polls every 15 s.
