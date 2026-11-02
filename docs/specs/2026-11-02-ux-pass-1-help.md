# First use: installation docs, help and doctor

This local order draft has two parts: one edit to open issue #1477 and one new size-s issue. It starts from main at `43a39c7b1b6edcfdaee18f0a87b606a720b889a8`. It publishes or approves nothing.

## Why this happens

README uses uv before explaining how to install it. Neither install section lists the GitHub repository and origin, GitHub CLI login, and Claude Code before the install command. The file still has the literal `GIF placeholder` and the claim dated 2026-10-29.

`src/foragent/cli.py` adds all discovered commands to one help list. It gives no first action. `src/foragent/commands/doctor.py` loads configuration before it gives startup guidance. The missing-project error in `src/foragent/config.py` says `.foragent.yml` is missing but names no next command.

The old draft combined this work with setup recovery and used an older main. That setup work is now owned by #1655, the `/fa` first run. This order contains no init recovery work and no edit to #1475. README must describe the prerequisites that current main requires.

## B. Explain installation before the first command

Edit [#1477](https://github.com/JinYoon-builds/foragent/issues/1477) in `edits[]`. Keep its original Goal paragraphs, Out of scope and approval checklist from the supplied current body. Its comparison-doc and old-note work remains part of that issue; this prototype proves only the README addition.

Update English and Korean install instructions together:

- Show how to install uv before the first command that needs it. A link to the official uv installation instructions is sufficient. Keep the existing Foragent installation recipe and Python explanation.
- Before installation, list a local Git repository with a GitHub repository connected as `origin`, GitHub CLI (`gh`) installed and logged in (`gh auth login`), and Claude Code installed and logged in. Codex CLI stays optional.
- Remove the literal `GIF placeholder` from visible text and comments. Remove the stale 208-PR claim dated 2026-10-29. Add no replacement count or recording promise.

Remove only this Done when command, with the same exact line and a reason in `done_when_removals`:

```sh
false  # placeholder: acceptance tests are written before this issue can be approved
```

It always exits 1 on clean main and cannot check the requested behavior. Replace it with the README acceptance command and the existing uv-install lock. Keep the original approval checklist. This replacement does not prove the broader comparison-doc or old-note requirements are complete.

## C. Show where to start

Create one new issue, size s, lane view.

`fa --help` separates Person commands from Internal commands and says `Start with fa init, then /fa.` The person commands are init, doctor, inspect and gate. Other discovered commands stay listed under Internal commands, including role, plan, orch, ci, tidy, test and bench. Every current command stays callable. Keep command discovery and subcommand help working.

Before init, `fa doctor` keeps the missing `.foragent.yml` explanation and names `fa init` as the next command. It returns a failure and leaves project files alone. After init, existing doctor checks keep working. Preserve the existing short error style that names the next command. Follow `docs/voice.md`; use the existing words and introduce no product terms.

This issue changes help and missing-project guidance only. Setup recovery, command names and other command behavior stay outside its scope. Lanes choose the implementation; the prototype is disposable evidence.

## Acceptance and verification

Start every existing acceptance file from this exact current main and keep every node ID. The two new files are absent on this main: retain all five node IDs from the B and C draft files. Remove the A acceptance file with its removed scope. Embed the two new files in `plan.json`; leave existing locks unchanged.

Prove each new file fails on clean main and passes with `prototype.patch`. Run every Done when command exactly as written, one file at a time. Done when uses plain `uv run pytest -q`; any Perl alarm belongs only to the local verification wrapper. Run the related init, doctor and CLI locks too. If a command fails on clean main for a reason outside this order, drop it from Done when and record why. Any removal of a line from #1477's current body needs `done_when_removals` with a reason.

Build the prototype from current main alone. It contains README, help and doctor changes only. Verify it applies cleanly and reproduces the tested files. Keep the shared checkout's src/, docs/, README, existing tests and index untouched. The final deliverable is the four files in `_drafts/ux-pass-1/`, uncommitted.

Real installation, GitHub setup, model sessions, `/fa`, publication and approval are not proved by these offline checks. Re-read #1477 before any later publication. Its broader original requirements still need evidence.
