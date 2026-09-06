"""MY-243: real Windows process evidence, with disposable board state only."""
from __future__ import annotations

import json
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import time

import psutil
import pytest

from hermes_cli import kanban_db as kb


@pytest.mark.windows_only
@pytest.mark.parametrize("concurrent_claim", [False, True])
def test_stop_never_started_task_without_inventing_an_exit(monkeypatch, concurrent_claim):
    kb.init_db()
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="disposable never-started stop", assignee="probe")
        assert conn.execute("SELECT COUNT(*) FROM task_runs WHERE task_id=?", (tid,)).fetchone()[0] == 0
        if concurrent_claim:
            survived = kb._worker_survived_termination
            def claim_before_stop_update(termination):
                assert kb.claim_task(conn, tid)
                return survived(termination)
            monkeypatch.setattr(kb, "_worker_survived_termination", claim_before_stop_update)
        result = kb.stop_task(conn, tid, reason="cancel before pickup")
        if concurrent_claim:
            assert result == {"stopped": False, "reason": "ownership_changed"}
            task = kb.get_task(conn, tid)
            assert task.status == "running" and task.claim_lock is not None
            assert task.current_run_id is not None
            return
        assert result["stopped"] is True
        assert result["never_started"] is True
        assert kb.get_task(conn, tid).status == "blocked"
        assert conn.execute("SELECT COUNT(*) FROM task_runs WHERE task_id=?", (tid,)).fetchone()[0] == 0
        assert kb.stop_task(conn, tid, reason="repeat cancellation")["stopped"] is True


@pytest.mark.windows_only
@pytest.mark.parametrize("case", ["prelaunch-only", "ambiguous-prior", "ambiguous-blocked", "concurrent-claim"])
def test_stop_failed_claim_requires_durable_prelaunch_proof(case, monkeypatch):
    kb.init_db()
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="disposable failed claim", assignee="default")
        for index in range(2):
            assert kb.claim_task(conn, tid)
            kb._record_spawn_failure(conn, tid, "disposable failure", failure_limit=2 if case == "ambiguous-blocked" else 10,
                                     launch_not_attempted=not (case.startswith("ambiguous") and index == 0))
        if case == "concurrent-claim":
            survived = kb._worker_survived_termination
            def claim_before_update(termination):
                assert kb.claim_task(conn, tid)
                return survived(termination)
            monkeypatch.setattr(kb, "_worker_survived_termination", claim_before_update)
        stopped = kb.stop_task(conn, tid, reason="park prelaunch failure")
        if case == "prelaunch-only":
            assert stopped["stopped"] is True and stopped["never_started"] is True
            assert kb.get_task(conn, tid).status == "blocked"
        else:
            assert stopped["stopped"] is False
            assert not stopped.get("never_started") or stopped.get("reason") == "ownership_changed"
        for attempt in conn.execute("SELECT * FROM task_runs WHERE task_id=?", (tid,)):
            assert attempt["worker_pid"] is None and attempt["worker_exited_at"] is None
            assert not attempt["worker_job_drained"]


def _dashboard_status_writer(root):
    spec = importlib.util.spec_from_file_location(
        "my244_windows_dashboard", root / "plugins/kanban/dashboard/plugin_api.py",
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module._set_status_direct


@pytest.mark.windows_only
@pytest.mark.parametrize("cooperative", [True, False], ids=["reaped-child", "orphan-child"])
@pytest.mark.parametrize("mode", ["contained", "legacy-uncontained", "legacy-stop", "supervisor-crash", "controller-stop", "controller-stop-running", "manual-reclaim", "ttl-reclaim", "heartbeat-reclaim", "runtime-reclaim", "legacy-reclaim", "crash-reclaim", "legacy-crash-reclaim", "capacity-crash-reclaim", "parent-reclaim", "parent-done-reclaim", "legacy-parent-reclaim", "parent-restart-reclaim", "parent-failed-reclaim", "dashboard-parent-reclaim", "dashboard-reclaim"])
def test_terminal_certificate_requires_the_complete_process_tree(cooperative, mode, monkeypatch):
    """A clean parent exit cannot certify a child that remains alive."""
    root = Path(__file__).resolve().parents[2]
    contained = not mode.startswith("legacy")
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
    sys.exit(int(sys.argv[2]) if reap_kanban_worker_descendants(0.2) else 17)
sys.exit(int(sys.argv[2]))
"""
    kb.init_db()
    conn = kb.connect()
    worker = None
    child = None
    try:
        parent = None
        if "parent" in mode:
            parent = kb.create_task(conn, title="disposable ancestor", assignee="probe")
            assert kb.complete_task(conn, parent)
        tid = kb.create_task(conn, title="MY-243 disposable Windows probe", assignee="probe",
                             parents=[parent] if parent else [])
        assert kb.claim_task(conn, tid)
        run_id = kb.get_task(conn, tid).current_run_id
        env = dict(os.environ, HERMES_KANBAN_TASK=tid, HERMES_KANBAN_STOP_NUDGE="1")
        command = [sys.executable, "-u", "-c", code, "reap" if cooperative else "orphan",
                   "75" if mode == "capacity-crash-reclaim" else "0"]
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
        reclaiming = mode.endswith("reclaim")
        if mode == "parent-done-reclaim" or (mode != "controller-stop-running" and not reclaiming):
            assert kb.complete_task(conn, tid, expected_run_id=run_id)
        assert kb.certify_terminal_worker_exits(conn) == []
        if parent:
            if mode.startswith("legacy"):
                with pytest.raises(RuntimeError, match="unverified"):
                    kb.invalidate_descendants_for_parent_reopen(conn, parent, author="test")
                assert kb.get_task(conn, tid).claim_lock is not None
                assert child.is_running()
                return
            if mode == "parent-failed-reclaim":
                from hermes_cli import kanban_worker_job
                with monkeypatch.context() as failure:
                    failure.setattr(kanban_worker_job, "terminate_job", lambda *_args: False)
                    with pytest.raises(RuntimeError, match="unverified"):
                        kb.invalidate_descendants_for_parent_reopen(conn, parent, author="test")
                assert kb.get_task(conn, tid).claim_lock is not None
                assert child.is_running()
                assert "descendant_invalidated" not in [e.kind for e in kb.list_events(conn, tid)]
            if mode == "parent-restart-reclaim":
                assert kb.prepare_descendant_stops_for_parent_reopen(conn, parent)
                conn.close()
                conn = kb.connect()
                assert kb.get_task(conn, tid).claim_lock is not None
                assert kb.get_task(conn, tid).status == "running"
            if mode == "dashboard-parent-reclaim":
                assert _dashboard_status_writer(root)(conn, parent, "todo")
                assert kb.get_task(conn, parent).status == "todo"
            else:
                result = kb.invalidate_descendants_for_parent_reopen(conn, parent, author="test")
                assert result["invalidated"][0]["id"] == tid
                assert result["terminations"] == []
            child.wait(timeout=5)
            worker.communicate(timeout=15)
            assert not child.is_running()
            assert kb.get_task(conn, tid).status == "todo"
            conn.close()
            conn = kb.connect()
            kinds = [event.kind for event in kb.list_events(conn, tid)]
            assert kinds.index("worker_stop_requested") < kinds.index("worker_stop_verified")
            assert kinds.index("worker_stop_verified") < kinds.index("descendant_invalidated")
            assert conn.execute("SELECT worker_job_exit_code FROM task_runs WHERE id=?", (run_id,)).fetchone()[0] == 1
            return
        if mode == "dashboard-reclaim":
            assert _dashboard_status_writer(root)(conn, tid, "ready")
            child.wait(timeout=5)
            worker.communicate(timeout=15)
            assert not child.is_running()
            assert kb.get_task(conn, tid).status == "ready"
            assert kb.get_task(conn, tid).claim_lock is None
            return
        if mode in ("crash-reclaim", "legacy-crash-reclaim", "capacity-crash-reclaim"):
            with kb.write_txn(conn):
                conn.execute("UPDATE tasks SET started_at=1 WHERE id=?", (tid,))
            worker.stdin.write("exit\n")
            worker.stdin.flush()
            if contained and not cooperative:
                psutil.Process(identity["worker"]).wait(timeout=5)
                assert child.is_running()
                assert kb.detect_crashed_workers(conn) == []
                assert kb.get_task(conn, tid).claim_lock is not None
                child.kill()
                child.wait(timeout=5)
            worker.communicate(timeout=15)
            kb._record_worker_exit_code(actual_pid, worker.returncode)
            if mode == "capacity-crash-reclaim":
                kb._recent_worker_exits.clear()
                conn.close()
                conn = kb.connect()
            recovered = kb.detect_crashed_workers(conn)
            if mode == "capacity-crash-reclaim":
                assert recovered == []
                assert tid in kb.detect_crashed_workers._last_capacity_wait
                assert kb.get_task(conn, tid).claim_lock is None
                assert kb.get_task(conn, tid).consecutive_failures == 0
                assert kb.get_run(conn, run_id).outcome == "capacity_wait"
            elif contained:
                assert tid in recovered
                assert kb.get_task(conn, tid).claim_lock is None
            else:
                assert recovered == []
                assert kb.get_task(conn, tid).claim_lock is not None
                if not cooperative:
                    assert child.is_running()
            return
        if reclaiming:
            if mode == "ttl-reclaim":
                with kb.write_txn(conn):
                    conn.execute("UPDATE tasks SET claim_expires=1, last_heartbeat_at=1 WHERE id=?", (tid,))
                result = kb.release_stale_claims(conn) == 1
            elif mode in ("heartbeat-reclaim", "runtime-reclaim"):
                with kb.write_txn(conn):
                    conn.execute("UPDATE tasks SET last_heartbeat_at=1, max_runtime_seconds=1 WHERE id=?", (tid,))
                    conn.execute("UPDATE task_runs SET started_at=1 WHERE id=?", (run_id,))
                result = tid in (kb.enforce_max_runtime(conn) if mode == "runtime-reclaim"
                                 else kb.detect_stale_running(conn, stale_timeout_seconds=1))
            else:
                result = kb.reclaim_task(conn, tid, reason="disposable full-tree reclaim")
            if mode == "legacy-reclaim":
                assert result is False
                assert child.is_running()
                assert kb.get_task(conn, tid).claim_lock is not None
                return
            assert result is True
            child.wait(timeout=5)
            worker.communicate(timeout=15)
            assert not child.is_running()
            assert worker.returncode != 0
            assert kb.get_task(conn, tid).claim_lock is None
            conn.close()
            conn = kb.connect()
            run = conn.execute("SELECT * FROM task_runs WHERE id=?", (run_id,)).fetchone()
            assert run["worker_job_drained"] == 1
            assert run["worker_job_exit_code"] == 1
            assert run["worker_exit_kind"] != "clean_exit"
            assert kb.certify_terminal_worker_exits(conn, tid) == [tid]
            proof = kb.get_run(conn, run_id)
            assert proof.worker_exit_code == 1
            assert proof.worker_exit_kind == "nonzero_exit"
            assert kb.certify_terminal_worker_exits(conn, tid) == []
            return
        if mode == "legacy-stop":
            stopped = kb.stop_task(conn, tid, reason="must not guess legacy process ownership")
            assert stopped["stopped"] is False
            assert stopped["reason"] == "process_tree_exit_unverified"
            assert child.is_running()
            assert kb.get_task(conn, tid).status == "done"
        if mode.startswith("controller-stop"):
            before = dict(conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone())
            stale = kb.stop_task(conn, tid, reason="stale exact-attempt stop", expected_run_id=run_id + 1)
            assert stale == {"stopped": False, "reason": "attempt_changed"}
            assert child.is_running()
            assert dict(conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()) == before
            stopped = kb.stop_task(conn, tid, reason="disposable full-tree stop", expected_run_id=run_id)
            assert stopped["stopped"] is True
            child.wait(timeout=5)
            worker.communicate(timeout=15)
            assert not child.is_running()
            assert worker.returncode != 0
            assert kb.get_task(conn, tid).status == "blocked"
            assert conn.execute(
                "SELECT worker_job_exit_code FROM task_runs WHERE id = ?", (run_id,),
            ).fetchone()[0] == 1
            conn.close()
            conn = kb.connect()
            assert kb.stop_task(conn, tid, reason="reopened stop replay", expected_run_id=run_id)["stopped"] is True
            assert kb.certify_terminal_worker_exits(conn, tid) == [tid]
            assert conn.execute(
                "SELECT worker_exit_kind FROM task_runs WHERE id = ?", (run_id,),
            ).fetchone()[0] != "clean_exit"
            return
        if mode == "supervisor-crash":
            payload = psutil.Process(identity["worker"])
            psutil.Process(worker.pid).kill()
            try:
                child.wait(timeout=5)
                payload.wait(timeout=5)
            finally:
                # Clean up the old implementation too when this regression fails.
                if payload.is_running():
                    payload.kill()
                    payload.wait(timeout=5)
            worker.communicate(timeout=15)
            assert not child.is_running()
            assert not payload.is_running()
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
