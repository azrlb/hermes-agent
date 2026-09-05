"""Tests for the ``on_kanban_worker_*`` observer hooks (RFC #58548).

Verifies the worker-lifecycle observers accepted in the #64231 batch
disposition: ``on_kanban_worker_spawned`` fires after ``spawn_fn`` returns
and the worker PID is durably persisted, ``on_kanban_worker_exited`` is
tick-derived from ``detect_crashed_workers`` and fires after every reclaim
transaction has committed, and ``on_kanban_worker_stale_claim`` fires when
``release_stale_claims`` reclaims a TTL-expired claim. All three are
observer-only, short-circuit on ``has_hook``, and can never break the
dispatcher.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.plugins import VALID_HOOKS, get_plugin_manager

WORKER_HOOKS = (
    "on_kanban_worker_spawned",
    "on_kanban_worker_exited",
    "on_kanban_worker_stale_claim",
)


@pytest.fixture
def empty_process_group(monkeypatch):
    """Isolate callback tests from OS containment (real Windows tests cover it)."""
    from hermes_cli import kanban_worker_job
    monkeypatch.setattr(kanban_worker_job, "job_is_empty", lambda *args: True)


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    # Crash detection acts immediately in these tests (no launch grace).
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.fixture
def captured_hooks(monkeypatch):
    """Register capturing callbacks for the worker-lifecycle hooks."""
    mgr = get_plugin_manager()
    events: list[tuple[str, dict]] = []
    saved = {k: list(v) for k, v in mgr._hooks.items()}
    for hook in WORKER_HOOKS:
        mgr._hooks.setdefault(hook, []).append(
            lambda _h=hook, **kw: events.append((_h, kw))
        )
    try:
        yield events
    finally:
        mgr._hooks = saved

def test_dispatch_spawn_fires_worker_spawned(
    kanban_home, all_assignees_spawnable, captured_hooks,
):
    """A dispatched spawn fires the hook AFTER the PID is durably persisted."""
    pid_at_fire_time: list = []

    def _read_pid(**kw):
        # Read through a FRESH connection: proves the PID write was
        # committed before the hook fired (the RFC timing contract).
        c2 = sqlite3.connect(kb.kanban_db_path())
        try:
            row = c2.execute(
                "SELECT worker_pid FROM tasks WHERE id = ?", (kw["task_id"],)
            ).fetchone()
            pid_at_fire_time.append(row[0] if row else None)
        finally:
            c2.close()

    mgr = get_plugin_manager()
    mgr._hooks.setdefault("on_kanban_worker_spawned", []).append(_read_pid)

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="t", assignee="alice")
        result = kb.dispatch_once(conn, spawn_fn=lambda *a, **k: 4242)
        assert any(row[0] == tid for row in result.spawned)
    finally:
        conn.close()

    fired = [e for e in captured_hooks if e[0] == "on_kanban_worker_spawned"]
    assert len(fired) == 1
    kw = fired[0][1]
    assert kw["task_id"] == tid
    assert kw["assignee"] == "alice"
    assert kw["worker_pid"] == 4242
    assert kw["workspace_path"]
    assert kw["run_id"] is not None
    assert "profile_name" in kw
    assert "board" in kw
    assert pid_at_fire_time == [4242]

def test_crash_reclaim_fires_worker_exited(kanban_home, captured_hooks, monkeypatch):
    """A dead-PID reclaim fires the exit observer with the exit facts."""
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="t", assignee="worker")
        kb.claim_task(conn, tid)
        kb._set_worker_pid(conn, tid, 98765)
        monkeypatch.setattr(kb, "_pid_alive", lambda pid: False)
        assert kb.detect_crashed_workers(conn) == [tid]
    finally:
        conn.close()

    fired = [e for e in captured_hooks if e[0] == "on_kanban_worker_exited"]
    assert len(fired) == 1
    kw = fired[0][1]
    assert kw["task_id"] == tid
    assert kw["assignee"] == "worker"
    assert kw["worker_pid"] == 98765
    assert kw["exit_kind"] == "unknown"
    assert kw["exit_code"] is None
    assert kw["outcome"] == "crashed"
    assert kw["retry_status"] == "ready"
    assert kw["run_id"] is not None
    assert "profile_name" in kw
    assert "board" in kw


def test_terminal_run_waits_for_exact_process_exit_then_certifies_once(
    kanban_home, captured_hooks, monkeypatch, empty_process_group,
):
    """A logical completion is not an exit; the later OS exit is durable."""
    conn = kb.connect()
    try:
        marker = {
            "controllerRunId": "controller-241", "eventSequence": 1,
            "dispatchId": "dispatch-241", "stateUrl": "https://controller/state",
            "submitUrl": "https://controller/submitLifecycleEvent",
        }
        tid = kb.create_task(
            conn, title="t", assignee="worker", idempotency_key="dispatch-241",
            body=f"<!-- codex-bmad-lifecycle {json.dumps(marker)} -->",
        )
        workspace = kb.workspaces_root() / f"scratch-{tid}"
        workspace.mkdir(parents=True)
        conn.execute(
            "UPDATE tasks SET workspace_kind = 'scratch', workspace_path = ? WHERE id = ?",
            (str(workspace), tid),
        )
        conn.commit()
        assert kb.claim_task(conn, tid)
        run_id = kb.get_task(conn, tid).current_run_id
        monkeypatch.setattr(kb, "_process_started_at", lambda pid: 1_000_123)
        kb._set_worker_pid(conn, tid, 98766)
        assert kb.complete_task(
            conn, tid, summary="done", expected_run_id=run_id,
        )
        assert workspace.exists()

        monkeypatch.setattr(kb, "_same_process_instance", lambda pid, started: True)
        assert kb.certify_terminal_worker_exits(conn) == []
        pending = kb.get_run(conn, run_id)
        assert pending.worker_pid == 98766
        assert pending.process_started_at == 1_000_123
        assert pending.worker_exited_at is None

        monkeypatch.setattr(kb, "_same_process_instance", lambda pid, started: False)
        monkeypatch.setattr(kb, "_classify_worker_exit", lambda pid: ("clean_exit", 0))
        assert kb.certify_terminal_worker_exits(conn) == [tid]
        assert kb.certify_terminal_worker_exits(conn) == []
        assert not workspace.exists()
        deliveries = []

        def transport(url, body):
            deliveries.append((url, body))
            return {"expectedEventSequence": 2} if url.endswith("/state") else {"accepted": True}

        assert kb.deliver_worker_exit_certificates(
            conn, key_id="hermes-lifecycle-v1", secret="lifecycle-secret", transport=transport,
        ) == [tid]
        assert kb.deliver_worker_exit_certificates(
            conn, key_id="hermes-lifecycle-v1", secret="lifecycle-secret", transport=transport,
        ) == []
        certified = kb.get_run(conn, run_id)
    finally:
        conn.close()

    assert certified.worker_exited_at is not None
    assert certified.worker_exit_code == 0
    assert certified.worker_exit_kind == "clean_exit"
    fired = [
        event for event in captured_hooks
        if event[0] == "on_kanban_worker_exited"
        and event[1].get("task_id") == tid
        and event[1].get("outcome") == "completed"
    ]
    assert len(fired) == 1
    assert fired[0][1]["run_id"] == run_id
    assert fired[0][1]["process_started_at"] == 1_000_123
    assert fired[0][1]["worker_exit_code"] == 0
    assert fired[0][1]["dispatch_id"] == "dispatch-241"
    assert len(fired[0][1]["certificate_id"]) == 64
    assert certified.worker_exit_delivered_at is not None
    assert certified.worker_exit_delivery_attempts == 1
    envelope = deliveries[1][1]
    signature = envelope.pop("signature")
    expected_signature = hmac.new(
        b"lifecycle-secret",
        json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    assert signature == expected_signature
    assert envelope["permissions"] == ["lifecycle"]
    assert envelope["payload"]["certificate"]["identity"]["dispatchId"] == "dispatch-241"


def test_exit_callback_retries_are_bounded_and_poll_proof_remains(
    kanban_home, monkeypatch, empty_process_group,
):
    marker = {
        "controllerRunId": "controller-241", "eventSequence": 1,
        "dispatchId": "dispatch-241", "stateUrl": "https://controller/state",
        "submitUrl": "https://controller/submitLifecycleEvent",
    }
    conn = kb.connect()
    try:
        tid = kb.create_task(
            conn, title="t", assignee="worker", idempotency_key="dispatch-241",
            body=f"<!-- codex-bmad-lifecycle {json.dumps(marker)} -->",
        )
        assert kb.claim_task(conn, tid)
        run_id = kb.get_task(conn, tid).current_run_id
        kb._set_worker_pid(conn, tid, 98768)
        assert kb.complete_task(conn, tid, expected_run_id=run_id)
        monkeypatch.setattr(kb, "_same_process_instance", lambda pid, started: False)
        monkeypatch.setattr(kb, "_classify_worker_exit", lambda pid: ("clean_exit", 0))
        assert kb.certify_terminal_worker_exits(conn) == [tid]

        def unavailable(url, body):
            raise RuntimeError("callback unavailable")

        for _ in range(kb._WORKER_EXIT_DELIVERY_LIMIT + 2):
            assert kb.deliver_worker_exit_certificates(
                conn, key_id="hermes-lifecycle-v1", secret="lifecycle-secret", transport=unavailable,
            ) == []
        run = kb.get_run(conn, run_id)
        events = kb.list_events(conn, tid)
    finally:
        conn.close()
    assert run.worker_exit_delivery_attempts == kb._WORKER_EXIT_DELIVERY_LIMIT
    assert run.worker_exit_delivered_at is None
    assert run.worker_exited_at is not None
    assert any(event.kind == "worker_exit_delivery_abandoned" for event in events)


def test_terminal_exit_with_unknown_code_is_not_reported_clean(
    kanban_home, monkeypatch, empty_process_group,
):
    """A lost/restarted watcher fails closed instead of inventing rc=0."""
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="t", assignee="worker")
        assert kb.claim_task(conn, tid)
        run_id = kb.get_task(conn, tid).current_run_id
        kb._set_worker_pid(conn, tid, 98767)
        assert kb.complete_task(conn, tid, expected_run_id=run_id)
        monkeypatch.setattr(kb, "_same_process_instance", lambda pid, started: False)
        monkeypatch.setattr(kb, "_classify_worker_exit", lambda pid: ("unknown", None))
        assert kb.certify_terminal_worker_exits(conn) == [tid]
        run = kb.get_run(conn, run_id)
    finally:
        conn.close()
    assert run.worker_exit_code == -1
    assert run.worker_exit_kind == "unknown"


def test_exit_certifier_ignores_crash_retries_and_preserves_blocked_workspace(
    kanban_home, monkeypatch, empty_process_group,
):
    """Only terminal handoffs are certified; blocked work remains available."""
    conn = kb.connect()
    try:
        crashed = kb.create_task(conn, title="crash", assignee="worker")
        assert kb.claim_task(conn, crashed)
        crash_run = kb.get_task(conn, crashed).current_run_id
        kb._set_worker_pid(conn, crashed, 98769)
        conn.execute(
            "UPDATE task_runs SET ended_at = ?, outcome = 'crashed' WHERE id = ?",
            (int(time.time()), crash_run),
        )

        blocked = kb.create_task(conn, title="blocked", assignee="worker")
        blocked_workspace = kb.workspaces_root() / f"scratch-{blocked}"
        blocked_workspace.mkdir(parents=True)
        conn.execute(
            "UPDATE tasks SET workspace_kind = 'scratch', workspace_path = ? WHERE id = ?",
            (str(blocked_workspace), blocked),
        )
        conn.commit()
        assert kb.claim_task(conn, blocked)
        blocked_run = kb.get_task(conn, blocked).current_run_id
        kb._set_worker_pid(conn, blocked, 98770)
        assert kb.block_task(
            conn, blocked, reason="needs review", expected_run_id=blocked_run,
        )

        monkeypatch.setattr(kb, "_same_process_instance", lambda pid, started: False)
        monkeypatch.setattr(kb, "_classify_worker_exit", lambda pid: ("clean_exit", 0))
        assert kb.certify_terminal_worker_exits(conn) == [blocked]
        assert kb.get_run(conn, crash_run).worker_exited_at is None
        assert kb.get_run(conn, blocked_run).worker_exited_at is not None
        assert blocked_workspace.exists()
    finally:
        conn.close()


def test_stale_claim_reclaim_fires_hook(kanban_home, captured_hooks):
    """A TTL-expired reclaim fires the stale-claim observer post-commit."""
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="t", assignee="worker")
        kb.claim_task(conn, tid)
        conn.execute(
            "UPDATE tasks SET claim_expires = ? WHERE id = ?",
            (int(time.time()) - 100, tid),
        )
        conn.commit()
        assert kb.release_stale_claims(conn) == 1
    finally:
        conn.close()

    fired = [e for e in captured_hooks if e[0] == "on_kanban_worker_stale_claim"]
    assert len(fired) == 1
    kw = fired[0][1]
    assert kw["task_id"] == tid
    assert kw["assignee"] == "worker"
    assert kw["worker_pid"] is None
    assert kw["heartbeat_stale"] is False
    assert kw["retry_status"] == "ready"
    assert kw["run_id"] is not None
    assert "profile_name" in kw
    assert "board" in kw

def test_raising_callbacks_never_break_worker_lifecycle(
    kanban_home, all_assignees_spawnable, monkeypatch,
):
    """Raising subscribers must not break spawn, crash reclaim, or stale reclaim."""
    mgr = get_plugin_manager()
    saved = {k: list(v) for k, v in mgr._hooks.items()}

    def _boom(**kw):
        raise RuntimeError("plugin exploded")

    for hook in WORKER_HOOKS:
        mgr._hooks.setdefault(hook, []).append(_boom)
    try:
        conn = kb.connect()
        try:
            tid = kb.create_task(conn, title="t", assignee="alice")
            result = kb.dispatch_once(conn, spawn_fn=lambda *a, **k: 111)
            assert any(row[0] == tid for row in result.spawned)

            monkeypatch.setattr(kb, "_pid_alive", lambda pid: False)
            assert kb.detect_crashed_workers(conn) == [tid]

            kb.claim_task(conn, tid)
            conn.execute(
                "UPDATE tasks SET claim_expires = ?, worker_pid = NULL "
                "WHERE id = ?",
                (int(time.time()) - 100, tid),
            )
            conn.commit()
            assert kb.release_stale_claims(conn) == 1
        finally:
            conn.close()
    finally:
        mgr._hooks = saved


def test_no_subscriber_short_circuits_worker_hooks(
    kanban_home, all_assignees_spawnable, monkeypatch,
):
    """With nothing registered, the new observers are never invoked at all."""
    from hermes_cli import lifecycle

    invoked: list[str] = []
    real_invoke = lifecycle.invoke_hook

    def _spy(hook_name, **kw):
        invoked.append(hook_name)
        return real_invoke(hook_name, **kw)

    monkeypatch.setattr(lifecycle, "invoke_hook", _spy)
    conn = kb.connect()
    try:
        kb.create_task(conn, title="t", assignee="alice")
        kb.dispatch_once(conn, spawn_fn=lambda *a, **k: 222)
    finally:
        conn.close()
    assert "on_kanban_worker_spawned" not in invoked
    # The shipped claimed hook has no short-circuit and still fires.
    assert "kanban_task_claimed" in invoked
