"""MY-243: real Windows process evidence, with disposable board state only."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time

import psutil
import pytest

from hermes_cli import kanban_db as kb


@pytest.mark.windows_only
@pytest.mark.parametrize("cooperative", [True, False], ids=["reaped-child", "orphan-child"])
@pytest.mark.parametrize("mode", ["contained", "legacy-uncontained", "supervisor-crash"])
def test_terminal_certificate_requires_the_complete_process_tree(cooperative, mode):
    """A clean parent exit cannot certify a child that remains alive."""
    root = Path(__file__).resolve().parents[2]
    contained = mode != "legacy-uncontained"
    code = """
import json, os, subprocess, sys
from agent.kanban_stop import reap_kanban_worker_descendants
child = subprocess.Popen(
    [sys._base_executable, '-c', 'import time; time.sleep(60)'],
    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    creationflags=subprocess.CREATE_NO_WINDOW,
)
print(json.dumps({'worker': os.getpid(), 'supervisor': os.getppid(), 'child': child.pid}), flush=True)
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
        command = [sys.executable, "-u", "-c", code, "reap" if cooperative else "orphan"]
        if contained:
            from hermes_cli.kanban_worker_job import prepare_worker_command
            command = prepare_worker_command(conn, tid, run_id, command)
        worker = subprocess.Popen(
            command,
            cwd=root, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, creationflags=subprocess.CREATE_NO_WINDOW,
        )
        identity = json.loads(worker.stdout.readline())
        actual_pid = worker.pid if contained else identity["worker"]
        child = psutil.Process(identity["child"])
        assert child.is_running()
        kb._set_worker_pid(conn, tid, actual_pid)
        assert kb.complete_task(conn, tid, expected_run_id=run_id)
        assert kb.certify_terminal_worker_exits(conn) == []
        if mode == "supervisor-crash":
            psutil.Process(worker.pid).kill()
            worker.stdin.write("exit\n")
            worker.stdin.flush()
            worker.communicate(timeout=15)
            kb._record_worker_exit_code(actual_pid, worker.returncode)
            assert kb.certify_terminal_worker_exits(conn) == []
            assert conn.execute(
                "SELECT worker_job_drained FROM task_runs WHERE id = ?", (run_id,),
            ).fetchone()[0] == 0
            return
        if contained and not cooperative:
            worker.stdin.write("exit\n")
            worker.stdin.flush()
            psutil.Process(identity["worker"]).wait(timeout=10)
            assert child.is_running()
            assert worker.poll() is None, "Supervisor must retain ownership of the orphan"
            assert kb.certify_terminal_worker_exits(conn) == []
            child.kill()
            child.wait(timeout=5)
        worker.communicate("exit\n", timeout=15)
        assert worker.returncode == 0
        kb._record_worker_exit_code(actual_pid, worker.returncode)
        if cooperative or contained:
            assert not child.is_running()
            assert kb.certify_terminal_worker_exits(conn) == ([tid] if contained else [])
        else:
            assert child.is_running(), "The test must establish an actual surviving child"
            assert kb.certify_terminal_worker_exits(conn) == [], (
                "Hermes certified a clean parent exit while its real Windows child survived"
            )
            child.kill()
            child.wait(timeout=5)
            # Reopen the database: proof survives a fresh observer connection.
            conn.close()
            conn = kb.connect()
            assert kb.certify_terminal_worker_exits(conn) == ([tid] if contained else [])
    finally:
        if child is not None and child.is_running():
            try:
                child.kill()
                child.wait(timeout=5)
            except psutil.NoSuchProcess:
                pass
        if worker is not None:
            if worker.poll() is None:
                worker.kill()
            worker.communicate(timeout=5)
        conn.close()


@pytest.mark.windows_only
def test_real_dispatch_launcher_retains_orphan_ownership(tmp_path, monkeypatch):
    """Exercise the shipped spawn + reap path; only the model payload is replaced."""
    root = Path(__file__).resolve().parents[2]
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    payload = """
import json, subprocess, sys
child = subprocess.Popen([sys._base_executable, '-c', 'import time; time.sleep(60)'],
    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    creationflags=subprocess.CREATE_NO_WINDOW)
print(json.dumps({'child': child.pid}), flush=True)
"""
    monkeypatch.setattr(kb, "_resolve_hermes_argv", lambda: [sys.executable, "-u", "-c", payload])
    kb.init_db()
    conn = kb.connect()
    child = None
    proc = None
    try:
        tid = kb.create_task(conn, title="disposable real launcher probe", assignee="probe")
        assert kb.claim_task(conn, tid)
        task = kb.get_task(conn, tid)
        pid = kb._default_spawn(task, str(root))
        proc = kb._live_worker_processes[pid]
        kb._set_worker_pid(conn, tid, pid)
        log_path = kb.worker_logs_dir() / f"{tid}.log"
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            lines = log_path.read_text().strip().splitlines()
            if lines:
                child = psutil.Process(json.loads(lines[-1])["child"])
                break
            time.sleep(0.05)
        assert child is not None, log_path.read_text()
        assert child.is_running()
        assert kb.complete_task(conn, tid, expected_run_id=task.current_run_id)
        assert kb.certify_terminal_worker_exits(conn) == []
        assert proc.poll() is None
        child.kill()
        child.wait(timeout=5)
        assert proc.wait(timeout=10) == 0
        assert pid in kb.reap_worker_zombies()
        # A restarted dispatcher has no process handles or in-memory exits.
        kb._recent_worker_exits.clear()
        conn.close()
        conn = kb.connect()
        assert kb.certify_terminal_worker_exits(conn) == [tid]
        assert kb.get_run(conn, task.current_run_id).worker_exit_code == 0
        assert kb.certify_terminal_worker_exits(conn) == []
    finally:
        if child is not None and child.is_running():
            try:
                child.kill()
                child.wait(timeout=5)
            except psutil.NoSuchProcess:
                pass
        if proc is not None:
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=5)
            kb._live_worker_processes.pop(proc.pid, None)
        conn.close()
