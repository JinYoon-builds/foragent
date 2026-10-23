# Stopping a run stops its whole process tree

Evidence (2026-10-23): after the gate stopped #138 at its time limit, 16 processes from its worktree kept running with parent launchd: the worker's own parallel pytest helper scripts (/private/tmp/fa-138-followup-*.py) and multiprocessing children. Machine load reached 12.8 on 8 CPUs and other runs timed out. The gate's stop ends the executor's process group only; descendants that start a new session survive, and nothing reaps them later.

- Stopping a run (time limit, stall) ends every process the run started, including descendants that left its process group or session.
- Each tick stops processes whose working directory or command line is inside the worktree of an issue that is not running, and records `kind=orphans-reaped` with the issue and count. Running issues' processes and processes outside Foragent worktrees are never touched.
