# P6 spec: `fa inspect` (data + answers + notification) and the `fa-inspect` skill

Builds on P1–P4. Product rules: `docs/inspect.md` (sections 1–5, D13), `docs/contract.md` 추가 8-4 (reason codes and groups), 5 (feedback goes through GitHub). Offline tests with FakeTracker.

## Principle
inspect makes no new judgments. It reads GitHub state left by gate and arranges it in the order a human acts. Human answers are written back to GitHub so the next `/fa-order` can read them. inspect never edits issue bodies and never adds `approved`.

## `src/foragent/inspect_data.py`
`collect(ctx) -> Inbox` where ctx has tracker, store, repo, config, now.
```python
@dataclass
class Item:
    group: str          # decision | reapprove | review | human-check | try | tech-failure
    number: int         # issue number
    title: str
    url: str            # issue or PR URL
    reason: str | None  # reason code when from needs-human
    summary: str        # bold first line of the gate comment, or a generated one
    details: str        # rest of the gate comment (question, options, failure tail), trimmed to 4000 chars
    pr: int | None
    answered: bool      # a decision answer comment newer than the needs-human comment exists
@dataclass
class Inbox:
    items: list[Item]   # ordered: decision, reapprove, review, human-check, try, tech-failure; within a group by number
    counts: dict[str, int]
    since: str | None   # last mark-seen time
```
Sources:
- Open issues labelled `needs-human`: the latest comment whose first line is a needs-human marker gives reason/summary/details. Group by `reasons.GROUP` (decision, reapprove, review, tech-failure). An issue with `needs-human` but no marker comment → group `decision`, summary "Needs a human (no Foragent comment found)".
- All open PRs whose head is `foragent/issue-<n>` and whose issue is not `needs-human` → group `review`, regardless of the global review mode (including path-specific holds).
- Merged Foragent PRs (head `foragent/issue-*`) merged after `since` → group `try` (one item per PR, url = PR). If the PR body has unchecked boxes under "Needs a human check" → also one `human-check` item listing them.
- `since` = last mark-seen time from the state store (`inspect.json`); first run uses 7 days ago.
Tracker additions as needed (FakeTracker + GhTracker): `list_prs(state="open"|"merged", head_prefix)`, returning number, head, title, body, merged_at, url. Use `gh api repos/{repo}/pulls?state=...` with pagination and filter locally.

## Answers (all write to GitHub, all idempotent where possible)
- `fa inspect answer N --text TEXT`: comment on issue N with first line `<!-- foragent:decision -->`, then `**Decision:**` and the text. Reuse an identical comment only when it is the latest decision after the current question. Keeps `needs-human` (the next `/fa-order` turns the decision into an edited issue and re-approval).
- `fa inspect retry N`: for tech-failure items only (refuse otherwise with a clear message): close any open PR with head `foragent/issue-N`, comment "Closed by Foragent retry", and delete its branch before removing `needs-human` so gate picks it up again; comment `<!-- foragent:retry -->` "Retry requested." Cleanup failure keeps the hold.
- `fa inspect feedback --title T --body B [--about N]`: create a new issue labelled `feedback` (body mentions `#N` when given). Add `create_issue(title, body, labels)` to the tracker.
- `fa inspect mark-seen`: store now as `since`.

## Notification
- `inspect_data.pending_signature(inbox) -> str`: stable hash of (group, number, reason) for items in decision/reapprove/review/human-check/tech-failure.
- `fa inspect notify [--repo-root]`: collect; if the signature differs from the last notified signature (stored in `inspect.json`) and there is at least one actionable item, send one line via macOS `osascript -e 'display notification "<line>" with title "Foragent <repo>"'` and print it; store the signature. The line: `"Decision 2 · Re-approve 1 · Review 3 · Try 5"` (omit zero groups). Notifier injectable; `notify` key in config: `None` or `"macos"` → macOS; anything else → print only (Slack later).
- Collection, signature comparison, delivery, and signature storage run under a blocking inspect-only flock at `<state>/notify.lock` so concurrent callers deliver once.
- At the end of `fa gate tick` (outside the tick lock, after the report), launch `fa inspect notify --repo-root ...` in a new session without waiting. Append both output streams to `<state>/notify.log`; spawn failures go to stderr without failing the tick.
- All GhTracker and GitRepo subprocess calls time out after 120 seconds (git fetch/push: 300 seconds); timeouts become TrackerError/GitError.

## CLI output
- `fa inspect [--json]`: human-readable inbox: one header line with counts, then items in order: `[decision] #3 Add tally report — <summary>` + indented details (first 12 lines) + url.

## `skills/fa-inspect/SKILL.md` (English, under 80 lines)
Steps for the agent running the skill:
1. Run `fa inspect --json`. If no items, say so in one line and stop.
2. Show one header line with the counts.
3. Go through items in order, one at a time:
   - decision: read details; present the question and the options it contains (keep the recommendation first and marked) with AskUserQuestion, plus "Later". On an answer, run `fa inspect answer N --text "<chosen option, plus any note>"`.
   - reapprove: show what changed; tell the user the approval step is adding the `approved` label on GitHub (give the link); do not add it yourself.
   - review: show the PR link and the summary; ask merge / close / later — merging and closing are done by the user on GitHub; the skill only records nothing.
   - human-check: list the unchecked items with the PR link; ask whether they pass; failures become `fa inspect feedback`.
   - try: show links; ask "anything that is not what you wanted?"; free text becomes `fa inspect feedback --about N`.
   - tech-failure: show the failure summary; ask retry / split later (feedback) / leave.
4. Finish with `fa inspect mark-seen` and a one-line recap of what was recorded.
Never edit issue bodies, never add `approved`, never merge.

## Tests
collect() ordering and grouping for every reason code; answered flag; PR-based review/try/human-check items and `since`; answer/retry/feedback/mark-seen effects and refusals; notify dedupe by signature, zero-item silence, notifier failure isolation from tick; CLI text and JSON shapes.

## Done criteria
`uv run pytest -q` passes. Report what you built and any ambiguity.
