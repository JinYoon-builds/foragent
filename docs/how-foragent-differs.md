# How foragent differs

People ask two questions first: "Isn't this just a goal loop?" and "Isn't this just hooks?" This page answers them. It also places foragent next to similar open-source projects.

## In one sentence

Foragent takes the tools of a single agent session (an orchestrator, subagents, a goal loop, hooks) and runs them across a whole project and over days, not hours. Code, called the gate, holds them together.

## Compared with a goal loop

A goal loop gives one agent one objective, and the agent keeps working until it decides it is done.

| | Goal loop | Foragent |
|---|---|---|
| Unit of work | One agent, one session | Several lanes at once |
| Who decides "done" | The agent itself | Acceptance tests locked at approval, plus an independent review. The agent cannot edit the tests. |
| When the session ends | The goal ends too | The gate resumes the work from the saved branch |
| Where work comes from | A person gives each goal | Lanes pull approved issues themselves |
| Conflicts with other work | Not considered | Declared and actually edited files decide who waits, with a per-file cap; every merge re-verifies on the latest main |
| Merge | A person merges | Verified work merges itself under a short merge lock (merge groups come after #459) |

A goal loop trusts the agent. Foragent is built so that you do not have to trust the agent. A goal loop is still useful inside foragent: it is a good way for one lane to finish one issue.

## Compared with hooks

Hooks run inside an agent session when the agent does something, for example before a tool call or when it tries to stop.

| | Hooks | Gate |
|---|---|---|
| Where it runs | Inside one agent session | Outside every session, as its own process |
| When it runs | Only when the agent acts | On a timer, even when no agent is running |
| Character | Reactive: it checks or blocks an action | Active: it starts work, assigns lanes, recovers stopped runs, merges, wakes the orchestrator |
| Scope | One session | Every lane in the repository |
| If the session dies | The hook dies with it | The gate keeps going |

The two work together. The gate installs hooks into lane sessions. For example, `fa gate check-done` runs the acceptance checks when an agent tries to stop, and keeps it working until they pass.

## Compared with an orchestrator plus subagents plus a goal loop

You can rebuild part of foragent in one session with an orchestrator agent that hands issues to subagents, each running a goal loop. These parts are what that setup lacks:

- **Persistence:** in that setup, everything stops when the orchestrator session ends or hits a limit. Foragent's state lives in files, and the gate continues.
- **A completion bar the agent cannot move:** foragent locks acceptance tests at approval.
- **Re-verify before merge:** foragent checks again on the latest main before it merges, under a short merge lock.
- **Machine-aware parallelism:** the number of runs at once follows CPU load, and critical memory pressure pauses new starts.
- **Saving interrupted work:** stopped work is committed and resumed, and a concurrent cleanup cannot delete it.
- **A light orchestrator:** results do not pile up in its context. It reads facts and makes decisions.

## Lanes

A lane is an agent that owns one area of the project.

- `fa init` creates the lanes and gives each one its skills.
- Each lane pulls its own issues.
- Each lane keeps its own memory, so over time it learns its area.

Isolation in git worktrees is standard in this field. What foragent adds on top is area ownership, lanes that learn, and a number of runs at once that follows machine load.

## Next to similar open-source projects (surveyed 2026-10-26)

| Layer | Examples | Merges without a person by default? |
|---|---|---|
| Company-level management | Paperclip | No; completion is a configured review or approval step |
| Orchestration and verification | Gas Town, OpenAI Symphony, Orchestrator.inc, foragent | Gas Town: yes, through a bisecting merge queue. Symphony: stops at Human Review. Orchestrator.inc: stops at "ready to merge". |
| Parallel launchers | Claude Squad, Vibe Kanban, Orca, Conductor | No; a person reviews diffs and merges |
| Single agents | Claude Code, Codex CLI, OpenCode, Goose, 가재코드 | No; one task per session |

None of the projects we surveyed combines all four of these:

1. acceptance tests locked at approval;
2. a re-verify on the latest main before merging;
3. lanes that own areas and learn;
4. a flow that runs on GitHub issues, Projects and PRs alone.

We judged this from READMEs and public docs, not from reading their code.

## Why merging without a person is not the headline

Merging without a person sounds risky, and most teams keep a person on the merge button on purpose. Foragent moves that person's attention instead of removing it. You review intent, meaning the issue's goal and its acceptance tests, before the work starts. You then try the result after it lands. The locked tests guard the middle. If you want a person to merge sensitive paths, list them under `always_human` in `.foragent.yml` or mark them `always_human` in the risk map; a person then decides those merges.

## Evidence

Foragent builds itself. From 2026-10-21 to 2026-10-26, its gate merged 173 pull requests in this repository.
