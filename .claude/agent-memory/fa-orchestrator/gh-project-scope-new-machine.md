---
name: gh-project-scope-new-machine
description: On a new machine the gh token may lack the project scope; fa order publish then commits tests and stops before creating issues
metadata:
  type: project
---

On 2026-11-04, on the new M4 Pro Mac mini, the gh token had only gist, read:org, repo and workflow. `fa order publish --approve` pushed the order commit (48c69e5) and then failed on board queries ("requires read:project"). No issues were created or approved. launchd.err.log already held 219 of the same errors from the gate.

**Why:** `gh auth login` on a new machine gives the default scopes only. Board reads and writes need `project`.

**How to apply:** after any machine move or re-login, check `gh auth status` for the `project` scope before publishing. If it is missing, ask Jin through the PM to run `gh auth refresh -h github.com -s project`; this is a credential, so the orchestrator cannot do it. Then publish the same plan again. Related: [[handoff-2026-11-04-new-mac]].
