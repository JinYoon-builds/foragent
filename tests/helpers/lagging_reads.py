"""GitHub reads that lag behind Foragent's own push (copied from the locked refresh-head test)."""




LAG_READS = 2


def lagging_reads(case, monkeypatch, pr):
    """GitHub shows the replaced head for the next LAG_READS reads after Foragent's next push.

    Only the first head change after this call lags; any later head change (for
    example another actor's push) is visible at once and ends the lag.
    """
    tracker = case.tracker
    set_head, head_of = tracker.set_pr_head_sha, tracker.pr_head_sha
    lag = {"armed": True, "replaced": None, "left": 0, "served": 0}

    def pushed(number, head):
        previous = head_of(number)
        set_head(number, head)
        if number != pr or previous == head:
            return
        if lag["armed"]:
            lag.update(armed=False, replaced=previous, left=LAG_READS)
        else:
            lag["left"] = 0

    def reported(number):
        if number == pr and lag["left"] > 0:
            lag["left"] -= 1
            lag["served"] += 1
            return lag["replaced"]
        return head_of(number)

    monkeypatch.setattr(tracker, "set_pr_head_sha", pushed)
    monkeypatch.setattr(tracker, "pr_head_sha", reported)
    return lag
