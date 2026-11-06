# lesson lock (2026-11-06)

main's ci has failed since daf8d353 (#2267): tests/test_skills_lanes.py send_lesson x2. Bisected locally: 8ad04102 passes, daf8d353 fails. Cause: file_locks.acquire adopts an exclusive hold by truncating the locked file and writing owner JSON; lanes.send_lesson locks the notebook itself. Every other caller locks a dedicated lock file. Every PR's CI fails until this lands.
