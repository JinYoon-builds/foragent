# Person-only commands refuse inside sessions FA launched

## Why

On 2026-11-03 Jin added the project permission rule `Bash(fa:*)`. Role sessions that run at the
repository root (architect, docs) now run every `fa` command with no confirmation. Nothing in the
code knows who is calling. The architect's "never approve" rule is only a sentence in its prompt.
Lane runs work in worktrees without that rule and with a deny list, but the deny list does not
name `fa`, so a lane session that is allowed to run `fa` has the same gap.

The root cause: FA launches lane, review, dream and role sessions without telling them, or the
commands they run, that FA launched them. The only mark that exists today is the orchestrator's
(`FORAGENT_ROLE=orchestrator`). Worse, a session started from the orchestrator inherits that
mark: an architect called by `fa orch architect`, or a lane run of a gate kicked from the
orchestrator session, looks exactly like the orchestrator.

## What changes

1. Every session FA launches through its executors (lane runs, reviews, dreams, role sessions
   such as architect and docs, with Claude or Codex) carries a fact in its environment saying FA
   launched it. That fact replaces any orchestrator mark the session would otherwise inherit.
   The lane picks the name and value. The orchestrator's existing mark does not change.
2. `fa` refuses these person-only commands when that fact is present, before it reads GitHub,
   starts a process or writes a file:
   - `fa orch resolve N --verdict ...`
   - `fa orch merge PR --approved-by ...`
   - `fa skills add URL --approve ...` (preview without `--approve` stays allowed)
   - `fa settings set ...` (`list` and `get` stay allowed)
   - `fa init --fa-allow yes|no`
   - `fa role add|set|remove`
   - `fa lanes skills LANE ...`
3. The refusal is one line on stderr, exit code non-zero, nothing on stdout. It says the
   command is for a person, that this session was started by FA, and that nothing changed. It
   uses the project language (en and ko).
4. A person's terminal and the PM session (no FA launch mark, with or without a session id)
   run these commands as today.
5. The orchestrator session keeps every verb its guide gives it today, including
   `fa order publish --approve`, `fa order flush`, `fa orch resolve --verdict`,
   `fa orch merge --approved-by` and `fa settings set` (see Decision below).

## Decision recorded

The order text says "a lane or role session". The orchestrator is also a role session, but its
guide (`skills/fa-orchestrator/SKILL.md`) has it record verdicts, merge dream PRs itself
(decision #189) and run `fa settings set` with no extra confirmation. This order keeps those
uses: it blocks every FA-launched session except the orchestrator. A test locks the
orchestrator's verdict path separately, so a later decision to narrow the orchestrator can drop
that one test.

## Out of scope

- Who may run `fa order publish --approve` and `fa order flush` (unchanged; the PM seat check
  from #1484 stays as is).
- `fa lanes talk` sessions, which a person drives directly.
- Clearing the orchestrator's `FORAGENT_SESSION_ID` that sessions it starts inherit today.
- Changing prompts, skills or the project permission rule.

## Tests

`tests/acceptance/test_person_only_commands.py` launches real lane and role sessions through
FA's launch paths with an offline stand-in for the `claude` and `codex` CLIs. The stand-in
records the environment it received; each command then runs inside exactly that environment.
No new variable name is prescribed. A command counts as refused only when it exits non-zero
with one stderr line and touches no GitHub client, no subprocess and no file.
