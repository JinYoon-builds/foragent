# Settings changed by talking

When the person states a setting in words ("turn off automatic merge", "use codex", "burn the
quota"), change it with the same writer the settings window uses: `fa settings set KEY VALUE`.

- Valid keys are exactly the ones `fa settings list` returns, with their scope (team, me, machine).
  "Turn off automatic merge" maps to `fa settings set review batch`.
- Do not ask for an extra confirmation; the person's words are the decision.
- Report the change to the person in the chat in one line. Say "whole team" only for team-scope keys,
  because those apply to everyone who uses the repository; me and machine keys affect only this person
  or this computer.
- An unknown key or a rejected value: report it in one line with the valid choices from
  `fa settings list` (or the command's error message). Change nothing.

## Team-scope keys

A team-scope key lives in `.foragent.yml`, which is in git. Commit it on the default branch without
touching the current checkout's branch or its other edits:

1. `git fetch origin <default>` and `git worktree add --detach <tmp> origin/<default>`.
2. In the temporary worktree run `fa settings set KEY VALUE --repo-root <tmp>`, so only that key in
   `.foragent.yml` changes.
3. Commit only `.foragent.yml` with a one-line message (for example `settings: review batch`) and
   `git push origin HEAD:<default>`.
4. Remove the temporary worktree after the push result is known.

If the push is refused, tell the person in one line that the change was not shared and why; leave
the default branch alone. The one-line report goes only to the person in the chat, not to issues.
