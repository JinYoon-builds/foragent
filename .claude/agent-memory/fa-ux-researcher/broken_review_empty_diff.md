---
name: broken-review-empty-diff
description: fa review Ctrl-C still prints a raw KeyboardInterrupt traceback and takes 8–24 s to exit; empty-target part fixed by #2215 (rechecked 2026-11-08)
metadata:
  type: project
---
Goal: maintainer reviews a contributor branch with `fa review <branch>` (README flow) and stops it with Ctrl-C.
Start: clone of the Foragent repo; branch with one committed change vs main.
Shortest repro: `fa review <branch>`, wait ~25–40 s, send SIGINT to its process group.
Observed (2026-11-08, fa ed9b0e6b): reviewer children (codex, playwright mcp) do stop, but fa prints a full KeyboardInterrupt traceback (ThreadPoolExecutor join) and exits after 7.6–23.5 s.
Expected: prompt exit with one plain line, no traceback.
Fixed (delete this part): empty target now prints "검토할 변경이 없어요…", exit 3, instantly; start line names target/base/merge base/file count.
Owner: review-engine lane, open issue #2489 (split from #2215). Recheck after #2489 merges; delete this entry when Ctrl-C exits cleanly.
