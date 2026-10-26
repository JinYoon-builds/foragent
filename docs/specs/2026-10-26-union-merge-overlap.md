# Union-merged paths are not overlap (2026-10-26)

After #448 (optimistic overlap, cap 2 per file), every run's records list .claude/agent-memory/fa-orch/MEMORY.md with 2-4 peers, because every run appends its index line. .gitattributes already merges that file with merge=union, so it cannot conflict. Overlap counting now reads the git merge attribute on the base and ignores union paths; exclusive still wins.
