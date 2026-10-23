# Skill packs and lane settings: two plumbing fixes

Found on 2026-10-23 while attaching pstack's principle-prove-it-works (D77 addendum 1).
1. `fa skills add https://github.com/cursor/plugins` aborted on one SKILL.md whose frontmatter does not parse (an unquoted description containing ": "), so none of the pack's valid skills could be added. A pack is added with its valid skills; each unreadable SKILL.md is reported under `skipped` with its repo-relative path and a one-line error. A pack with no valid skill still fails and names every bad file.
2. `fa lanes skills` (and `fa lanes add`) rewrite `.foragent.yml` through a YAML dump and drop comments. It dropped the line that recorded the paused board id. Lane commands change only the lines they must; every comment and every other line keep their bytes and order.
