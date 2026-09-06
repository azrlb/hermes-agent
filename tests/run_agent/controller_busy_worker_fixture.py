"""Disposable busy process tree stopped by the actual controller gateway."""

import json
import os
from pathlib import Path
import subprocess
import sys
import time

import psutil
import pytest


def exercise_busy_worker_cancel(assignment, control_url, post):
    from hermes_cli import kanban_db as kb
    from hermes_cli.profiles import get_profile_dir

    workspace = Path(assignment['worktreePath'])
    task_id = assignment['hermesTaskId']
    ready = workspace / 'busy-ready.json'
    evidence = workspace / 'busy-evidence.txt'
    heartbeat = workspace / 'busy-heartbeat.txt'
    child_program = "import time\nfrom pathlib import Path\np=Path('busy-heartbeat.txt')\nwhile True:\n p.write_text(str(time.time_ns()))\n time.sleep(0.05)\n"
    compile(child_program, 'busy-child', 'exec')
    payload = """
import json, os, subprocess, sys, time
from pathlib import Path
import psutil
Path('busy-evidence.txt').write_text('preserve busy worker output\\n')
child = subprocess.Popen([sys.executable, '-c', CHILD_PROGRAM], creationflags=subprocess.CREATE_NO_WINDOW)
Path('busy-ready.json').write_text(json.dumps({'pid': child.pid, 'started': psutil.Process(child.pid).create_time()}))
while True:
    time.sleep(0.1)
""".replace('CHILD_PROGRAM', repr(child_program))
    compile(payload, 'busy-parent', 'exec')
    worker = None
    with kb.connect_closing(board='probe') as conn:
        task = kb.get_task(conn, task_id)
        assert task.status == 'ready' and task.current_run_id is None
        profile = get_profile_dir(task.assignee)
        assert profile.is_relative_to(Path(os.environ['HERMES_HOME']))
        profile.mkdir(parents=True, exist_ok=True)
        try:
            with pytest.MonkeyPatch.context() as patched:
                patched.setattr(kb, '_resolve_hermes_argv', lambda: [sys.executable, '-u', '-c', payload])
                dispatched = kb.dispatch_once(conn, board='probe', max_spawn=1)
            assert [item[0] for item in dispatched.spawned] == [task_id], dispatched
            task = kb.get_task(conn, task_id)
            attempt = task.current_run_id
            worker = kb._worker_processes[task.worker_pid]
            deadline = time.monotonic() + 20
            while not (ready.exists() and heartbeat.exists()) and time.monotonic() < deadline:
                assert worker.poll() is None, (kb.worker_logs_dir(board='probe') / f'{task_id}.log').read_text()
                time.sleep(0.05)
            assert ready.exists() and heartbeat.exists(), (kb.worker_logs_dir(board='probe') / f'{task_id}.log').read_text()
            child = json.loads(ready.read_text())
            assert psutil.Process(child['pid']).create_time() == child['started']
            before = heartbeat.read_text()
            time.sleep(0.15)
            assert heartbeat.read_text() != before, 'child must be actively writing before cancellation'
            mode = assignment['controlMode']
            assert post(control_url, {'taskId': task_id, 'attempt': attempt})['status'] == ('cancelled' if mode == 'cancel' else 'held')
            assert worker.wait(timeout=15) != 0
            if psutil.pid_exists(child['pid']):
                assert psutil.Process(child['pid']).create_time() != child['started'], 'owned child survived cancellation'
            assert kb.get_task(conn, task_id).status == 'blocked'
            run = conn.execute('SELECT worker_job_drained, worker_job_exit_code, worker_exit_kind FROM task_runs WHERE id = ?', (attempt,)).fetchone()
            assert run['worker_job_drained'] == 1
            assert run['worker_job_exit_code'] == 1
            assert run['worker_exit_kind'] != 'clean_exit'
            observer = """
import json,sys
from dataclasses import asdict
from hermes_cli import kanban_db as kb
with kb.connect_closing(board='probe') as conn:
    kb.certify_terminal_worker_exits(conn, sys.argv[1])
    print(json.dumps(asdict(kb.get_run(conn, int(sys.argv[2])))))
"""
            proof = json.loads(subprocess.check_output([sys.executable, '-c', observer, task_id, str(attempt)],
                env=dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[2])), text=True, timeout=20))
            assert proof['worker_exit_kind'] == 'nonzero_exit' and proof['worker_exit_code'] == 1
            assert proof['worker_pid'] == worker.pid
            assert proof['worker_exited_at'] >= proof['process_started_at']
            assert evidence.read_text() == 'preserve busy worker output\n'
            assert subprocess.check_output(['git', '-C', str(workspace), 'rev-parse', 'HEAD'], text=True).strip() == assignment['request']['baseCommit']
            final = heartbeat.read_text()
            time.sleep(0.15)
            assert heartbeat.read_text() == final
            if mode != 'cancel':
                result = post(control_url + '-finished', {'taskId': task_id, 'attempt': attempt})
                deadline = time.monotonic() + 90
                while result['status'] == 'pending' and time.monotonic() < deadline:
                    time.sleep(0.25)
                    result = post(control_url + '-finished-result', {})
                assert result['status'] == 'verified', result
                assert heartbeat.read_text() == final
                assert evidence.read_text() == 'preserve busy worker output\n'
        finally:
            if worker is not None and worker.poll() is None:
                stopped = kb.stop_task(conn, task_id, reason='dispose failed busy-worker test')
                assert stopped['stopped'] is True
                worker.wait(timeout=15)
