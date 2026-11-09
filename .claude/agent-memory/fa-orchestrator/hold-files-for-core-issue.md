---
name: hold-files-for-core-issue
description: a core issue rebased again and again by base-changed refreshes lands once its files are held exclusive (overlap 1); 11-09 #2503 merged ~1 h after the hold, after 2 d 20 h and three refreshes
metadata:
  type: feedback
---

#2503 (structure cleanup A core) ran 2 d 20 h with three base-changed refreshes: other issues kept landing on its files.
Jin r1312 (11-09 10:3x KST): hold its files. `fa orch plan --overlap PATH=1 ... --by orchestrator` on the 13 concrete
files (globs are not accepted) stopped new starts there; #2503 merged at 11:34. Keeping the same limit until the next
priority issue (#2506, same files) merges also enforced Jin's order without blocked_by on existing issues.

**Why:** a long issue on hot files loses every race to small issues that land first and force a refresh.
**How to apply:** when a priority issue is refreshed onto a moved base twice, propose holding its files exclusive (it is
reversible: PATH=off); expand globs to real files with git ls-tree; keep the file list in drafts so the lift is exact.
