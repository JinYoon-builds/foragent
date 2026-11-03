"""Owner-only read snapshots. A final publication/repair boundary starts a fresh snapshot."""

from contextlib import contextmanager

from foragent.tracker import GhTracker, RecordingTracker, RejectedEvidence


class _GhSnapshot(GhTracker):
    _artifact_error = RejectedEvidence

    def _call(self, args, input=None, *, binary=False):
        # Only explicit GETs and the read-only checks command are memoized.
        read = (args[0] == "api" and args[1] != "graphql" and "-X" not in args
                and "--method" not in args and input is None
                or args[:2] == ["pr", "checks"])
        key = (tuple(args), binary)
        if not read or self.cache is None:
            return super()._call(args, input, binary=binary)
        if key not in self.cache:
            self.cache[key] = super()._call(args, input, binary=binary)
        return self.cache[key]


class Snapshot:
    READS = {"get_pr", "pr_head_sha", "get_issue", "parent_of", "pr_changed_files", "pr_checks",
             "pr_acceptance_reports", "pr_default_branch", "list_issues"}

    def __init__(self, tracker):
        while isinstance(tracker, RecordingTracker):
            tracker = tracker._tracker
        if type(tracker) is GhTracker:
            tracker = _GhSnapshot(tracker.repo, run=tracker._run, sleep=tracker._sleep)
            tracker.cache = None
        self.tracker, self.cache = tracker, None

    def clear(self):
        if self.cache is not None:
            self.cache.clear()
        if isinstance(self.tracker, _GhSnapshot) and self.tracker.cache is not None:
            self.tracker.cache.clear()

    @contextmanager
    def fresh(self):
        previous = self.cache
        self.cache = {}
        raw_previous = None
        if isinstance(self.tracker, _GhSnapshot):
            raw_previous, self.tracker.cache = self.tracker.cache, {}
        try:
            yield
        finally:
            self.cache = previous
            if isinstance(self.tracker, _GhSnapshot):
                self.tracker.cache = raw_previous

    def __getattr__(self, name):
        call = getattr(self.tracker, name)
        if name in {"close_pr", "merge_pr", "update_pr", "close_issue", "add_labels", "remove_labels"}:
            def write(*args, **kwargs):
                try:
                    return call(*args, **kwargs)
                finally:
                    self.clear()
            return write
        if name not in self.READS or self.cache is None:
            return call

        def read(*args, **kwargs):
            key = (name, args, tuple(sorted(kwargs.items())))
            if key not in self.cache:
                self.cache[key] = call(*args, **kwargs)
            return self.cache[key]

        return read
