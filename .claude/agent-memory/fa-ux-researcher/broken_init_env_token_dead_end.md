---
name: broken-init-env-token-dead-end
description: fa init with an invalid GH_TOKEN env var says "saved login" and points to gh auth login, which gh refuses (confirmed 2026-11-06)
metadata:
  type: project
---

Goal: a new user finishes `fa init` while the shell exports an old, invalid GH_TOKEN.
Start: fresh git repo with a GitHub origin, empty GH_CONFIG_DIR, fa ed15ca1a (after #2197/PR #2222).
Repro: `GH_CONFIG_DIR=<empty> GH_TOKEN=ghp_invalid... fa init --language en|ko`
Observed: "gh has a saved login, but its token is invalid or expired." + "gh-logged-in: gh auth login". GH_TOKEN is never named. `gh auth login` then refuses: "The value of the GH_TOKEN environment variable is being used...".
Reproduced again on 2026-11-06 (fa ed15ca1a, en and ko) and sent with fa orch request (status: reported). Owner: init lane; next: the orchestrator decides whether to order it. Retest; remove when the message names GH_TOKEN.
