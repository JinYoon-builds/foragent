import sys, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent / "acceptance"))
import test_unsaved_worktree_bytes_are_kept as t
from test_stop_whole_tree import fake_processes  # noqa
from test_stop_whole_tree import events

def test_probe(git_history, tmp_path, monkeypatch, fake_processes, capsys):
    orig = t._copies
    def spy(case, content):
        for e in events(case):
            print("EV", json.dumps(e)[:400], file=sys.__stderr__)
        print("LOG", case.store.log_path(1).read_text()[-3000:], file=sys.__stderr__)
        for p in case.store.root.rglob("*"):
            if "worktrees" not in str(p): print("F", p, file=sys.__stderr__)
        return orig(case, content)
    monkeypatch.setattr(t, "_copies", spy)
    from foragent.git import GitRepo
    real = GitRepo.keep_unsaved_copy
    def wrapped(self, dest):
        print("STATUS", repr(self._call(["status", "--porcelain=v1", "-z", "--untracked-files=all"])), file=sys.__stderr__)
        print("LSF", repr(self._call(["ls-files", "-s", "-v"])), file=sys.__stderr__)
        print("HEADTREE", repr(self._call(["ls-tree", "-r", "HEAD"])), file=sys.__stderr__)
        return real(self, dest)
    monkeypatch.setattr(GitRepo, "keep_unsaved_copy", wrapped)
    t.test_failed_wip_save_keeps_unsaved_bytes_before_worktree_removal(git_history, tmp_path, monkeypatch, fake_processes, capsys, "stale-index-lock")
