"""Locked: concurrent public prereads retain both completed answers."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
from pathlib import Path
from threading import Event, current_thread

from foragent import executors, order
from foragent.executors import ExecResult
from test_order import plan, publishing  # noqa: F401
from test_publish_out_of_scope import publish_cli
from test_skills_autosync import cli_exit, isolated_home  # noqa: F401


def test_two_prereads_retain_both_answers_and_both_approval_gates(publishing, plan, monkeypatch):
    """Reject atomic replacement with lost updates, a shared temp name."""
    root, config, tracker, git, _ = publishing
    plan['issues'] = plan['issues'][:1]
    second = deepcopy(plan)
    second.update(id='second-order', title='Second order')
    second['issues'][0]['goal'] = 'A distinct receipt outcome.'
    paths = [root / 'one.json', root / 'two.json']
    for path, data in zip(paths, (plan, second), strict=True):
        path.write_text(json.dumps(data))
    calls = []

    class Reader:
        def run(self, **kwargs):
            calls.append(kwargs['prompt'])
            return ExecResult(0, False, 'NONE', kwargs['log_path'])

    monkeypatch.setattr(executors, 'executor_for', lambda name: Reader())
    monkeypatch.setattr(order, 'executor_for', lambda name: Reader())
    main = publish_cli(publishing, monkeypatch)
    reached, release, second_done = Event(), Event(), Event()
    original = Path.write_text
    paused = [False]

    def delayed(path, text, *args, **kwargs):
        if (current_thread().name.startswith('first-preread') and path.name.startswith('preread.json')
                and not paused[0]):
            paused[0] = True
            reached.set()
            assert release.wait(10)
        return original(path, text, *args, **kwargs)

    monkeypatch.setattr(Path, 'write_text', delayed)

    def read(path, done=None):
        try:
            return cli_exit(main, ['order', 'preread', str(path), '--repo-root', str(root), '--json'])
        finally:
            if done:
                done.set()

    with ThreadPoolExecutor(1, thread_name_prefix='first-preread') as first:
        first_result = first.submit(read, paths[0])
        reached.wait(0.3)
        with ThreadPoolExecutor(1) as peer:
            second_result = peer.submit(read, paths[1], second_done)
            second_done.wait(0.3)
            release.set()
            assert first_result.result(timeout=10) == 0
            assert second_result.result(timeout=10) == 0
    assert len(calls) == 2, 'Both implementers must actually preread'
    records = json.loads((root / '.foragent/preread.json').read_text())
    assert {row['plan'] for row in records.values()} == {plan['id'], second['id']}, 'One successful concurrent preread was lost'
    # Verify the externally meaningful consequence, without publishing or bypassing preread.
    for data in (plan, second):
        order.validate_plan(data, root, config)
        order.require_clean_preread(data, root, config, data['issues'])
