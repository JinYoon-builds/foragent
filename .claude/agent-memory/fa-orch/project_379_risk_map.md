---
name: project-379-risk-map
description: #379 risk map (.foragent-risks.yml) — where the reviewer, tier, always_human and fa risk CLI hook in; command-module import rule
metadata:
  type: project
---
- `risks.py` reads the map at the base ref (`load_at(git, ref)` via blob_sha + `cat-file blob`), so a PR cannot rewrite the risks that review it. CLI (`fa risk`) reads/writes the working-tree file at `--repo-root`.
- Review: `run_review._review()` matches `git.changed_paths(base_ref)`; matched → notes in Codex prompt / CLAUDE.local.md scope / PR-body "## Review scope" (`run_publish._render_pr_body`), plus tier l (`codex_effort` as `effort`, Claude `claude_model or reviewer_model`). No match → byte-identical old behaviour.
- always_human: `policy.merge_decision(risk_always_human=...)`, every run_publish call site passes `_risk_always_human(ctx)` (map at `origin/<base_branch>`).
- `tests/test_cli_commands.py` forbids any module-level import in `commands/*.py` except `facade`/`repo_store`; import stdlib inside functions.
- Review fixes: notes must show every entry field (always_human, added by/at/event too). PR-body notes sit between `<!-- foragent:risk-notes -->` markers; `risks.refresh_notes()` swaps them on every review-only re-review, because Claude pr-body review gets an empty prompt and reads only the PR body.
