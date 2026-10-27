"""Exercise the signal boundary with stubs, never a real signal sender."""

import os
import signal
import subprocess

import pytest

from conftest import no_real_signals


@pytest.mark.parametrize("module,name,target", [
    (os, "kill", (42000,)),
    (os, "killpg", (42000,)),
    (signal, "pidfd_send_signal", (100,)),
    (signal, "pthread_kill", (42000,)),
    (signal, "raise_signal", ()),
])
def test_guard_allows_probes_and_blocks_delivery(monkeypatch, module, name, target):
    calls = []
    result = object()

    def sender(*args):
        calls.append(args)
        return result

    # Reinstall over a stub so even a broken guard cannot deliver a real signal.
    with monkeypatch.context() as patch:
        patch.setattr(module, name, sender, raising=False)
        no_real_signals.__wrapped__(patch)
        guarded = getattr(module, name)
        for signum in (signal.SIGTERM, signal.SIGKILL, signal.SIGINT, -1):
            with pytest.raises(pytest.fail.Exception, match="Real delivering signals are forbidden"):
                guarded(*target, signum)
        assert calls == []
        assert guarded(*target, 0) is result
        assert calls == [(*target, 0)]


def test_guard_preserves_probe_errors(monkeypatch):
    def missing(pid, signum):
        raise ProcessLookupError(pid)

    with monkeypatch.context() as patch:
        patch.setattr(os, "kill", missing)
        no_real_signals.__wrapped__(patch)
        with pytest.raises(ProcessLookupError):
            os.kill(42000, 0)


def test_guard_allows_subprocess_launches():
    with subprocess.Popen(["true"]) as child:
        assert child.wait() == 0
