"""Preread rules that make one round enough, and the per-issue round count shown by fa order progress."""

from foragent.state import StateStore


# Each extra round is a full preread: ask for everything at once, after checking the whole issue.
RULES = (
    "Before you answer, check the whole issue: compare its Files with the code the change must touch, and\n"
    "read its statements, Out of scope and the locked tests the change would break.\n"
    "List every unknown you can find in one reply; each later round costs a full preread.\n"
    "A choice inside the issue's Files that does not change WHAT (a setting name, field name, file layout,\n"
    "internal record or which helper to reuse) is yours: write it as a DECIDE line instead of a question.\n"
)

ROUNDS = "preread_rounds"


def count_round(store: StateStore, key: str) -> None:
    """Add one finished preread of issue ``key`` to the order in preparation; no order, no count."""

    def update(state):
        if state is not None:
            rounds = state.setdefault(ROUNDS, {})
            rounds[key] = rounds.get(key, 0) + 1
        return state

    store.update_order_progress(update)
