"""Behavior coverage for neutral provider-capacity waits on kanban workers."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def test_worker_exit_contract_only_neutrally_retries_transient_provider_failures():
    for reason in (
        "rate_limit",
        "upstream_rate_limit",
        "overloaded",
        "server_error",
        "timeout",
    ):
        assert (
            kb.kanban_worker_exit_code(
                {"failed": True, "failure_reason": reason}, True
            )
            == kb.KANBAN_CAPACITY_WAIT_EXIT_CODE
        )

    for reason in ("billing", "authentication", "permission", "unknown"):
        assert kb.kanban_worker_exit_code(
            {"failed": True, "failure_reason": reason}, True
        ) == 1

    assert kb.kanban_worker_exit_code({"failed": False}, True) == 0
    assert kb.kanban_worker_exit_code(
        {"failed": True, "failure_reason": "timeout"}, False
    ) == 1


def test_reaper_observes_registered_popen_exit_cross_platform():
    proc = subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.exit(75)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    with kb._worker_processes_lock:
        kb._worker_processes[proc.pid] = proc

    deadline = time.time() + 5
    reaped = []
    while time.time() < deadline and proc.pid not in reaped:
        reaped.extend(kb.reap_worker_zombies())
        if proc.pid not in reaped:
            time.sleep(0.02)

    assert proc.pid in reaped
    assert kb._classify_worker_exit(proc.pid) == (
        "capacity_wait",
        kb.KANBAN_CAPACITY_WAIT_EXIT_CODE,
    )


def test_durable_exit_record_survives_gateway_memory_loss(
    kanban_home, monkeypatch,
):
    task_id = "t_restart_capacity"
    run_id = 42
    pid = 54321
    path = kb._worker_exit_record_path(task_id, run_id)
    monkeypatch.setenv("HERMES_KANBAN_EXIT_RECORD", str(path))
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
    monkeypatch.setattr(kb.os, "getpid", lambda: pid)

    kb.write_kanban_worker_exit_record(kb.KANBAN_CAPACITY_WAIT_EXIT_CODE)
    kb._recent_worker_exits.clear()

    assert kb._classify_durable_worker_exit(task_id, run_id, pid) == (
        "capacity_wait",
        kb.KANBAN_CAPACITY_WAIT_EXIT_CODE,
    )


def test_detect_crashed_worker_neutrally_requeues_from_durable_record(
    kanban_home, monkeypatch,
):
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)

    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="capacity", assignee="coder")
        claimed = kb.claim_task(conn, task_id)
        assert claimed is not None and claimed.current_run_id is not None
        pid = 987654
        conn.execute(
            "UPDATE tasks SET worker_pid=?, consecutive_failures=0 WHERE id=?",
            (pid, task_id),
        )
        conn.commit()

        path = kb._worker_exit_record_path(task_id, claimed.current_run_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "taskId": task_id,
                    "runId": claimed.current_run_id,
                    "pid": pid,
                    "exitCode": kb.KANBAN_CAPACITY_WAIT_EXIT_CODE,
                    "finishedAt": int(time.time()),
                }
            ),
            encoding="utf-8",
        )
        kb._recent_worker_exits.clear()

        assert task_id not in kb.detect_crashed_workers(conn)
        task = kb.get_task(conn, task_id)
        assert task is not None
        assert task.status == "ready"
        assert task.consecutive_failures == 0
        assert "temporary provider capacity" in (task.last_failure_error or "")

        run = conn.execute(
            "SELECT outcome FROM task_runs WHERE task_id=? ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        event = conn.execute(
            "SELECT kind FROM task_events WHERE task_id=? ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        assert run["outcome"] == "capacity_wait"
        assert event["kind"] == "capacity_wait"
        assert not path.exists()


def test_capacity_wait_cooldown_defers_then_allows_probe(
    kanban_home, monkeypatch,
):
    monkeypatch.setenv("HERMES_KANBAN_RATE_LIMIT_COOLDOWN_SECONDS", "300")
    now = 6_000_000
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="capacity guard", assignee="coder")
        claimed = kb.claim_task(conn, task_id)
        assert claimed is not None and claimed.current_run_id is not None
        conn.execute(
            "UPDATE task_runs SET outcome='capacity_wait', "
            "status='capacity_wait', ended_at=? WHERE id=?",
            (now, claimed.current_run_id),
        )
        conn.execute(
            "UPDATE tasks SET status='ready', current_run_id=NULL, "
            "claim_lock=NULL, claim_expires=NULL, worker_pid=NULL, "
            "last_failure_error='temporary provider capacity' WHERE id=?",
            (task_id,),
        )
        conn.commit()

        monkeypatch.setattr(kb.time, "time", lambda: now + 100)
        assert kb.check_respawn_guard(conn, task_id) == "capacity_wait_cooldown"
        monkeypatch.setattr(kb.time, "time", lambda: now + 400)
        assert kb.check_respawn_guard(conn, task_id) is None


def test_mismatched_durable_record_is_counted_as_a_real_crash(
    kanban_home, monkeypatch,
):
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="bad record", assignee="coder")
        claimed = kb.claim_task(conn, task_id)
        assert claimed is not None and claimed.current_run_id is not None
        pid = 876543
        conn.execute("UPDATE tasks SET worker_pid=? WHERE id=?", (pid, task_id))
        conn.commit()
        path = kb._worker_exit_record_path(task_id, claimed.current_run_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "taskId": "different-task",
                    "runId": claimed.current_run_id,
                    "pid": pid,
                    "exitCode": kb.KANBAN_CAPACITY_WAIT_EXIT_CODE,
                    "finishedAt": int(time.time()),
                }
            ),
            encoding="utf-8",
        )

        assert task_id in kb.detect_crashed_workers(conn)
        task = kb.get_task(conn, task_id)
        assert task is not None
        assert task.consecutive_failures == 1


def test_stale_or_future_exit_record_is_rejected(kanban_home):
    task_id = "t_bad_time"
    run_id = 9
    pid = 13579
    path = kb._worker_exit_record_path(task_id, run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    base = {
        "version": 1,
        "taskId": task_id,
        "runId": run_id,
        "pid": pid,
        "exitCode": kb.KANBAN_CAPACITY_WAIT_EXIT_CODE,
    }
    path.write_text(
        json.dumps({**base, "finishedAt": 100}), encoding="utf-8"
    )
    assert kb._classify_durable_worker_exit(
        task_id, run_id, pid, run_started_at=1_000
    ) == ("unknown", None)

    path.write_text(
        json.dumps({**base, "finishedAt": int(time.time()) + 1_000}),
        encoding="utf-8",
    )
    assert kb._classify_durable_worker_exit(
        task_id, run_id, pid, run_started_at=1_000
    ) == ("unknown", None)


def test_dispatcher_supplies_exact_durable_exit_record_path(
    kanban_home, monkeypatch, tmp_path,
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    captured = {}

    class FakePopen:
        pid = 456789

        def __init__(self, _cmd, **kwargs):
            captured["cmd"] = _cmd
            captured["env"] = kwargs["env"]

        def poll(self):
            return None

    monkeypatch.setattr(kb.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(kb, "_resolve_hermes_argv", lambda: ["hermes"])
    task = kb.Task(
        id="t_exit_record",
        title="worker",
        body=None,
        assignee="coder",
        status="running",
        priority=0,
        created_by=None,
        created_at=0,
        started_at=0,
        completed_at=None,
        workspace_kind="scratch",
        workspace_path=str(workspace),
        claim_lock="host:worker",
        claim_expires=100,
        tenant=None,
        current_run_id=17,
    )
    try:
        assert kb._default_spawn(task, str(workspace)) == FakePopen.pid
        assert captured["env"]["HERMES_KANBAN_EXIT_RECORD"] == str(
            kb._worker_exit_record_path(task.id, 17)
        )
        assert "-Q" in captured["cmd"]
    finally:
        with kb._worker_processes_lock:
            kb._worker_processes.pop(FakePopen.pid, None)


def test_non_default_board_restart_classifies_before_expired_claim_reclaim(
    kanban_home, monkeypatch,
):
    board = "capacity-board"
    kb.create_board(board)
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)

    with kb.connect(board=board) as conn:
        task_id = kb.create_task(conn, title="board wait", assignee="coder")
        claimed = kb.claim_task(conn, task_id, ttl_seconds=1)
        assert claimed is not None and claimed.current_run_id is not None
        pid = 765432
        conn.execute(
            "UPDATE tasks SET worker_pid=?, claim_expires=? WHERE id=?",
            (pid, int(time.time()) - 10, task_id),
        )
        conn.commit()
        path = kb._worker_exit_record_path(
            task_id, claimed.current_run_id, board=board
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "taskId": task_id,
                    "runId": claimed.current_run_id,
                    "pid": pid,
                    "exitCode": kb.KANBAN_CAPACITY_WAIT_EXIT_CODE,
                    "finishedAt": int(time.time()),
                }
            ),
            encoding="utf-8",
        )
        result = kb.dispatch_once(conn, board=board, max_spawn=0)

        assert result.reclaimed == 0
        assert result.capacity_wait == [task_id]
        assert result.rate_limited == [task_id]
        assert kb.get_task(conn, task_id).status == "ready"


def test_goal_loop_returns_the_last_turn_failure_for_exit_classification(
    kanban_home, monkeypatch,
):
    import cli as cli_module
    from hermes_cli import goals

    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="goal",
            body="finish the work",
            assignee="coder",
            goal_mode=True,
        )
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)

    later_failure = {"failed": True, "failure_reason": "timeout"}
    fake_agent = SimpleNamespace(
        session_id="session-1",
        run_conversation=lambda **_kwargs: later_failure,
    )
    fake_cli = SimpleNamespace(
        agent=fake_agent,
        conversation_history=[],
        session_id="session-1",
    )

    monkeypatch.setattr(
        goals,
        "judge_goal",
        lambda *_args, **_kwargs: (
            "continue", "more work", False, None, False
        ),
    )
    result = cli_module._run_kanban_goal_loop_q(
        fake_cli,
        "first response",
        {"failed": False, "final_response": "first response"},
    )
    assert result is later_failure
    assert kb.kanban_worker_exit_code(result, True) == 75
    with kb.connect() as conn:
        assert kb.get_task(conn, task_id).status == "ready"


def test_goal_loop_does_not_judge_or_block_a_first_turn_capacity_wait(
    kanban_home, monkeypatch,
):
    import cli as cli_module
    from hermes_cli import goals

    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="goal first wait",
            body="finish the work",
            assignee="coder",
            goal_mode=True,
        )
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setattr(
        goals,
        "judge_goal",
        lambda *_args, **_kwargs: pytest.fail("capacity wait must not be judged"),
    )
    first_failure = {"failed": True, "failure_reason": "timeout"}
    fake_agent = SimpleNamespace(
        session_id="session-1",
        run_conversation=lambda **_kwargs: pytest.fail(
            "capacity wait must not spend another turn"
        ),
    )
    fake_cli = SimpleNamespace(
        agent=fake_agent,
        conversation_history=[],
        session_id="session-1",
    )

    result = cli_module._run_kanban_goal_loop_q(
        fake_cli, "", first_failure
    )
    assert result is first_failure
    with kb.connect() as conn:
        assert kb.get_task(conn, task_id).status == "ready"
