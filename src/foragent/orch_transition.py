"""A transition compares one observed issue state with its intended result."""

from dataclasses import dataclass, replace


@dataclass(frozen=True)
class TransitionState:
    labels: frozenset[str]
    state: str
    blocked_reason: tuple[str, str | None] | None
    hint: str | None

    def after(self, *, add=(), remove=(), **fields):
        return replace(self, labels=(self.labels | frozenset(add)) - frozenset(remove), **fields)


def transition_state(ctx, n: int, issue=None) -> TransitionState:
    """Read once, before effects. No comment, park history or delivery queue is evidence.

    Verbs pass their opening issue observation. Runs use the existing local issue
    projection, so deciding whether to comment adds no GitHub calls. RecordingTracker
    maintains that projection for delivered writes and explicit undelivered intent alike.
    """
    if issue is None:
        row = next((row for row in ctx.store.local_issues() if row["number"] == n), None)
        if row is None:
            issue = ctx.tracker.get_issue(n)
        else:
            labels, state = frozenset(row["labels"]), row["state"]
    if issue is not None:
        labels, state = frozenset(issue.labels), issue.state
    flag = ctx.store.load_flag(n)
    path = ctx.store.root / "hints" / f"{n}.txt"
    try:
        hint = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        hint = None
    return TransitionState(labels, state, (flag.reason, flag.summary) if flag else None, hint)
