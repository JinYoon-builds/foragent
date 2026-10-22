# P8a spec: deterministic tools behind `/fa-order` and `/fa-init`, plus difficulty tiers

The `/fa-order` skill (written separately, `skills/fa-order/SKILL.md`) does the interview and judgment. It calls these CLI commands for every deterministic step. Read `docs/order.md`, `docs/decisions.md` D11, D18–D26, `docs/contract.md`, and `src/foragent/*.py`. Offline tests (FakeTracker, temp git repos, a fake library directory).

## 1. `fa order context [--repo-root] --json`
One JSON document the skill reads before interviewing:
- `language`, `lanes` (name → paths, agent, skills), `tiers`, `acceptance_dir`, `base_branch`.
- `feedback`: open issues labelled `feedback` (number, title, body, about).
- `decisions`: open `needs-human` issues with reason `spec-question` or `no-machine-check`: number, title, current body, the gate question (comment details), the latest `<!-- foragent:decision -->` answer if any.
- `open_issues`: number, title, labels, lane, size, state group (from `fa gate status` logic).
- `specs`: paths and first heading of `docs/specs/*.md`.
- `repo`: top-level tree (depth 2, ignore .git/.venv/node_modules), detected languages/test commands (pyproject → `uv run pytest`, package.json scripts.test → `npm test`/`pnpm test`, go.mod → `go test ./...`), whether `.github/workflows` exists.

## 2. `fa order publish PLAN.json [--repo-root] [--approve] [--dry-run]`
PLAN.json schema (validate strictly, clear errors):
```json
{
  "id": "2026-10-20-login",            // plan id, used for idempotency
  "title": "Login and payments MVP",   // parent issue title
  "summary": "…",                      // parent issue body text (human language)
  "spec_markdown": "…",                // saved to docs/specs/<id>.md
  "new_lanes": [{"name": "api", "paths": ["api/**"], "skills": ["engineering/testing-strategy"]}],
  "issues": [
    {"key": "login-api", "title": "…", "goal": "…", "out_of_scope": "…",
     "done_when": ["uv run pytest -q tests/acceptance/test_login_api.py"],
     "checklist": ["…"], "lane": "api", "size": "m",
     "blocked_by": ["other-key"], "acceptance_files": {"tests/acceptance/test_login_api.py": "<file content>"}}
  ],
  "edits": [{"number": 3, "goal": "…", "out_of_scope": "…", "done_when": ["…"], "checklist": [], "acceptance_files": {}}]
}
```
Steps (each idempotent; a re-run of the same `id` updates instead of duplicating — store the key→issue number map and per-issue acceptance blob SHAs in the parent issue body as an HTML comment `<!-- foragent:plan id=… map={json} acceptance={"issue-number":{"path":"blob-sha"}} -->`; continue reading legacy comments without `acceptance`):
1. Validate: every issue has ≥1 done_when command; every acceptance file is under `acceptance_dir`; every done_when command that references the acceptance dir references a file present in `acceptance_files` or already in the repo; `blocked_by` keys exist and form no cycle; lanes exist or are in `new_lanes`; size ∈ {s,m,l}.
2. New lanes: `fa lanes add` logic (section 3) for each.
3. Acceptance files + spec: write them on the base branch, verify with `git ls-files` that all files are tracked (force-add ignored files; fail if still absent), commit "Foragent order <id>: acceptance tests and spec" (only these paths), verify every file has a blob in that commit, then push (explicit refspec). This happens **before** issues are created (contract 8-2).
4. Parent issue (create or update) with summary and a checklist of children.
5. Child issues: body in the contract format (`<!-- foragent:v1 -->`, `## Goal`, `## Out of scope`, `## Done when` with one ```sh block per command, checklist items, and a link line per acceptance file), labels `lane:<lane>` (omit for `main`) and `size:<s|m|l>`; native sub-issue link to the parent; native blocked-by edges (resolve keys to numbers) — add `Tracker.add_sub_issue(parent, child)` and `Tracker.add_blocked_by(n, blocker)` (GhTracker: `POST repos/{repo}/issues/{parent}/sub_issues -F sub_issue_id=<id>`, `POST repos/{repo}/issues/{n}/dependencies/blocked_by -F issue_id=<id>`; ignore "already exists").
   On republication, reconcile blocked-by edges with the plan, removing obsolete edges through `Tracker.remove_blocked_by(n, blocker)` (`DELETE repos/{repo}/issues/{n}/dependencies/blocked_by/{issue_id}`). Compare each issue's acceptance blobs with its last published snapshot, independently of local writes; changes remove `approved` and `ready` before optional reapproval. Missing legacy snapshots with acceptance files also require reapproval.
6. Edits: rewrite the existing issue body in the contract format (preserve title), write its acceptance files in step 3's commit, remove `needs-human` and `approved` (the edit invalidates approval anyway), comment `<!-- foragent:edited -->` "Updated by order from your decision; needs approval."
7. `--approve`: add `approved` to every created/edited child (v1 approval = the human's label through their own token, D14; the skill only passes `--approve` after the human chose "approve all" or listed which to approve — support `--approve-keys k1,k2` and `--approve-numbers 3,5` too).
8. Print a summary table grouped by lane: number, title, size, blocked by, approved?
`--dry-run` validates and prints the plan without any write.
Ensure labels `size:s|m|l` exist (add to `labels.LABELS`).

## 3. Lanes and skills
- `fa lanes add NAME --paths GLOB... [--skill LIB:SKILL ...] [--executor] [--model]`: add to `.foragent.yml` `lanes` (never overwrite an existing lane; error), create `.claude/agents/fa-<name>.md`:
  ```
  ---
  name: fa-<name>
  description: Foragent lane "<name>": implements issues in <paths>.
  memory: project
  skills: [<installed skill names>]
  ---
  You implement one GitHub issue at a time in this repository's <name> lane (<paths>).
  Record durable, project-specific lessons (patterns, pitfalls, commands) in your memory.
  ```
  and commit the configuration, agent, every installed skill file, and `THIRD_PARTY_NOTICES.md` (only these paths) with message "Foragent lane <name>", then push the base branch. Order publication includes these files when recovering an interrupted lane commit and pushes once after acceptance files are committed.
- Skill libraries: `fa skills sync` clones or fast-forwards the defaults `https://github.com/anthropics/knowledge-work-plugins` and `https://github.com/anthropics/skills` into `~/.local/share/foragent/libraries/<name>` (configurable through `skill_libraries`). Discover both `*/skills/*/SKILL.md` and `skills/*/SKILL.md`. `fa skills catalog [--json]` lists `{id, name, description, license, role, runs_scripts, copies}` with stable IDs such as `kwp:design/design-system` and `skills:frontend-design`. Unprefixed and previous library-qualified IDs are accepted only when unique. Copies under `.<tool>/skills/<name>` merge with `skills/<name>`; the shared copy takes precedence for installation. `copies` lists every relative directory, `runs_scripts` reports script files across all copies, and `role` is null until confirmed.
- `fa skills add SOURCE [--json]` previews a Git repository from a temporary checkout without writing configuration or library caches. `fa skills add SOURCE --approve --role SKILL=ROLE ...` registers it globally only when every skill has an explicit role (`style`, `review`, `tool`, or `domain`). Names or full skill IDs select roles; ambiguous names require full IDs. Roles live in `skill_roles` keyed by the catalog IDs. Existing settings and roles survive registration, and adding the same repository again keeps one library entry. Registration leaves downloading the permanent cache to catalog/install. This command reads metadata and file paths; it never runs skill scripts.
- `fa skills install ID --repo-root` checks the skill folder's `LICENSE`/`LICENSE.txt`, falling back to the library root. Recognized Apache-2.0, MIT, BSD and ISC licenses permit installation; source-available and unknown licenses are refused with a reason. Copy the skill directory into `.claude/skills/<skill>/` without overwriting different local contents, and record the source, commit, and selected license text in `THIRD_PARTY_NOTICES.md`. `fa lanes add --skill` uses the same check before writing lane files.
- Lane skill counts have no cap. Codex receives `Lane skills (read the file when relevant and follow it):` followed by `- <name>: <one-line description> (.claude/skills/<name>/SKILL.md)` for each installed skill; skill bodies stay in their files. Missing files are skipped and recorded as `missing skill: <name>` in the PR body. Claude receives its lane through `--agent`.

## 4. Difficulty tiers (D26) in gate
- Config `tiers` (default):
  ```yaml
  tiers:
    s: {codex_effort: medium, claude_model: null}
    m: {codex_effort: high,   claude_model: null}
    l: {codex_effort: xhigh,  claude_model: null}
  ```
  Codex effort goes to `-c model_reasoning_effort=<effort>`; `claude_model` (when set) to `--model`. Lane executor/model still take precedence for executor and model; tiers adjust effort.
- Issue size from its `size:` label (default `m`). Attempt k starts at the issue tier and escalates one tier per failed attempt (s→m→l, capped). Record the tier per attempt in the run record and the PR body ("Attempt 2 at tier l").

## Tests
context JSON (all sections, test-command detection); publish: validation errors, dry-run no writes, ordering (acceptance commit before issues), contract body exactness, labels, sub-issues and blocked-by resolution, idempotent re-run by id, edits invalidate approval and clear needs-human, --approve variants, lane creation; lanes add files and refusal on existing lane; skills sync/catalog/install with a fake library dir, NOTICE content, refusal on conflicting skill; tiers mapping and escalation per attempt.

## Done criteria
`uv run pytest -q` passes. Report what you built and any ambiguity.
