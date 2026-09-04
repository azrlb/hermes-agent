"""MY-243: real Windows process evidence, with disposable board state only."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import psutil
import pytest

from hermes_cli import kanban_db as kb


@pytest.mark.windows_only
@pytest.mark.parametrize("cooperative", [True, False], ids=["reaped-child", "orphan-child"])
def test_terminal_certificate_requires_the_complete_process_tree(cooperative):
    """A clean parent exit cannot certify a child that remains alive."""
    root = Path(__file__).resolve().parents[2]
    code = """
import json, os, subprocess, sys
from agent.kanban_stop import reap_kanban_worker_descendants
child = subprocess.Popen(
    [sys._base_executable, '-c', 'import time; time.sleep(60)'],
    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    creationflags=subprocess.CREATE_NO_WINDOW,
)
print(json.dumps({'worker': os.getpid(), 'child': child.pid}), flush=True)
sys.stdin.readline()
if sys.argv[1] == 'reap':
    sys.exit(0 if reap_kanban_worker_descendants(0.2) else 17)
"""
    kb.init_db()
    conn = kb.connect()
    worker = None
    child = None
    try:
        tid = kb.create_task(conn, title="MY-243 disposable Windows probe", assignee="probe")
        assert kb.claim_task(conn, tid)
        run_id = kb.get_task(conn, tid).current_run_id
        env = dict(os.environ, HERMES_KANBAN_TASK=tid, HERMES_KANBAN_STOP_NUDGE="1")
        worker = subprocess.Popen(
            [sys.executable, "-u", "-c", code, "reap" if cooperative else "orphan"],
            cwd=root, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, creationflags=subprocess.CREATE_NO_WINDOW,
        )
        identity = json.loads(worker.stdout.readline())
        actual_pid = identity["worker"]
        child = psutil.Process(identity["child"])
        assert child.is_running()
        kb._set_worker_pid(conn, tid, actual_pid)
        assert kb.complete_task(conn, tid, expected_run_id=run_id)
        assert kb.certify_terminal_worker_exits(conn) == []
        worker.communicate("exit\n", timeout=15)
        assert worker.returncode == 0
        kb._record_worker_exit_code(actual_pid, worker.returncode)
        if cooperative:
            assert not child.is_running()
            assert kb.certify_terminal_worker_exits(conn) == [tid]
        else:
            assert child.is_running(), "The test must establish an actual surviving child"
            assert kb.certify_terminal_worker_exits(conn) == [], (
                "Hermes certified a clean parent exit while its real Windows child survived"
            )
    finally:
        if child is not None and child.is_running():
            child.kill()
            child.wait(timeout=5)
        if worker is not None:
            if worker.poll() is None:
                worker.kill()
            worker.communicate(timeout=5)
        conn.close()
