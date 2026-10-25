# Waiting phases are not stalls (Jin, 2026-10-26)

watch_layout.py shows watch_stalled when the worker's last activity is older than min(10, stall_minutes) minutes. In checking, CI-wait, merge-lock or test-slot waits the worker is idle by design while the runner works, so healthy runs (#450, #379, #497, #123) showed as stalled.
