# Expected files for queued issues (D69)

Each queued issue now lists the files it is expected to change, so the gate can add a second worker to a crowded
lane when files do not overlap (#145). Estimated by reading each issue's acceptance test and the code it exercises.
Issues touching runner logic list `src/foragent/runner*.py` and `src/foragent/run_*.py` because #143 splits runner.py.
#143 itself has no files (it touches the whole runner) and is never cloned.
