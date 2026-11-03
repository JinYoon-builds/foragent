# hot_overlap_files re-fire (2026-11-04)

r425, r434, r438 each started an architect session; each had one file just at the threshold and +1/+2 counts. Decided by the orchestrator (reversible signal tuning): inside the window, new-at-threshold files are drift; doubling or after-window new files call again; hot_overlap_files only.
