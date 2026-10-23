# D79: adapted pstack procedures for the orchestrator and bug issues

See docs/decisions.md D79. Source: pstack by Lauren Tan (MIT), https://github.com/cursor/plugins/tree/9b93e81fb2921b9263d9463c15bafeabfe3903aa/pstack — `skills/poteto-mode/playbooks/{bug-fix,investigation,refactoring,perf-issue}.md` and `skills/principle-fix-root-causes`. A local copy is at `~/.local/share/foragent/sources/pstack-skills/` (playbooks/ and skills/).

- Write skills/fa-orchestrator/references/{bug-fix,investigate,refactor,performance}.md: keep the skeleton (reproduce → rule hypotheses out with evidence, no guessing, plant logging when unsure → plan → confirm at the same place → failing test first → report with evidence), drop Cursor-only tooling (`/loop`, control skills, specific models) and pstack-internal skill names, credit the source and MIT license in each file.
- skills/fa-orchestrator/SKILL.md: for blockers and repeated failures (D77), measure before acting; when evidence refutes a hypothesis, revert what it motivated; link the four references.
- An issue labeled `bug` gets the bug-fix procedure's key steps in its implementer prompt; other issues do not.
- Judge the effect on the D77 scorecard before and after; remove what does not help.
