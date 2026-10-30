# #370 redesign (2026-10-27)

#370 was held repeatedly because the stop command and the live runner both wrote the same run. Under D95 the runner owns the run while it lives; the stop command records its intent first and acts only after the runner's process tree is confirmed dead.
