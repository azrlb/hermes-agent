"""Turn-end guard for kanban workers.

Kanban workers must end with ``kanban_complete`` or ``kanban_block``. Models
(especially GLM / Qwen families) sometimes narrate the next step
("Let me write the report now") and stop with ``finish_reason=stop`` and no
tool calls. Hermes treats that as a clean exit → ``rc=0`` → dispatcher
``protocol_violation``.

This module is policy-only: when a kanban worker tries to finish without a
terminal board tool, return a bounded synthetic nudge so the conversation
loop continues instead of exiting.
"""

from __future__ import annotations

import json
import os
from typing import Any, Iterable, Optional


_TERMINAL_KANBAN_TOOLS = frozenset({"kanban_complete", "kanban_block"})

_DEFAULT_MAX_ATTEMPTS = 2


def kanban_stop_nudge_enabled() -> bool:
    """Return whether the kanban stop-guard is active for this process.

    On when ``HERMES_KANBAN_TASK`` is set (dispatcher-spawned worker), unless
    ``HERMES_KANBAN_STOP_NUDGE`` explicitly disables it.
    """
    env = os.environ.get("HERMES_KANBAN_STOP_NUDGE")
    if env is not None and env.strip().lower() in {"0", "false", "no", "off"}:
        return False
    task = (os.environ.get("HERMES_KANBAN_TASK") or "").strip()
    return bool(task)


def _tool_call_name(tc: Any) -> str:
    if isinstance(tc, dict):
        fn = tc.get("function")
        if isinstance(fn, dict):
            return str(fn.get("name") or "")
        return str(tc.get("name") or "")
    fn = getattr(tc, "function", None)
    if fn is not None:
        return str(getattr(fn, "name", "") or "")
    return str(getattr(tc, "name", "") or "")


def session_called_kanban_terminal(messages: Iterable[dict] | None) -> bool:
    """True if this conversation already invoked a terminal kanban tool."""
    if not messages:
        return False
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        if role == "assistant":
            for tc in msg.get("tool_calls") or []:
                if _tool_call_name(tc) in _TERMINAL_KANBAN_TOOLS:
                    return True
        elif role == "tool":
            name = str(msg.get("name") or "")
            if name in _TERMINAL_KANBAN_TOOLS:
                return True
    return False


def session_succeeded_kanban_terminal(messages: Iterable[dict] | None) -> bool:
    """True only after a terminal tool returned its structured success result."""
    if not messages:
        return False
    for msg in reversed(list(messages)):
        if not isinstance(msg, dict) or msg.get("role") != "tool":
            continue
        if str(msg.get("name") or "") not in _TERMINAL_KANBAN_TOOLS:
            continue
        content = msg.get("content")
        try:
            result = json.loads(content) if isinstance(content, str) else content
        except (TypeError, ValueError):
            return False
        return isinstance(result, dict) and result.get("ok") is True
    return False


def worker_attempt_is_terminal() -> bool:
    """Recognize completion by a child CLI without trusting its stdout.

    The task alone is insufficient: a previous attempt or a reopened task must
    never authorize this worker's successful exit. Read the exact inherited
    attempt and latest task state together from one database snapshot.
    """
    task_id = os.environ.get("HERMES_KANBAN_TASK", "").strip()
    run_id = os.environ.get("HERMES_KANBAN_RUN_ID", "").strip()
    if not task_id or not run_id.isdecimal() or int(run_id) < 1:
        return False
    try:
        from hermes_cli import kanban_db as kb

        with kb.connect_closing() as conn:
            row = conn.execute(
                "SELECT t.status AS task_status, r.outcome AS outcome "
                "FROM tasks t JOIN task_runs r ON r.task_id = t.id "
                "WHERE t.id = ? AND r.id = ? AND r.ended_at IS NOT NULL "
                "AND t.current_run_id IS NULL "
                "AND r.id = (SELECT MAX(id) FROM task_runs WHERE task_id = t.id)",
                (task_id, int(run_id)),
            ).fetchone()
        return row is not None and (row["task_status"], row["outcome"]) in {
            ("done", "completed"), ("blocked", "blocked"),
        }
    except Exception:
        # Missing, unreadable or conflicting state is never completion proof.
        return False


def reap_kanban_worker_descendants(timeout_seconds: float = 3.0) -> bool:
    """Stop every descendant before a terminal kanban worker exits.

    The worker has already recorded its terminal result and will run no more
    model turns, so its background subprocesses have no legitimate work left.
    Re-scan to a fixed point because one child can briefly create another while
    termination is in progress.  False means at least one descendant survived;
    the caller must make the worker exit abnormally so the controller holds.
    """
    if not kanban_stop_nudge_enabled():
        return True
    try:
        import psutil
        parent = psutil.Process(os.getpid())
        for _attempt in range(3):
            children = parent.children(recursive=True)
            if not children:
                return True
            for child in reversed(children):
                try:
                    child.terminate()
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
            _gone, alive = psutil.wait_procs(children, timeout=timeout_seconds)
            for child in alive:
                try:
                    child.kill()
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
            psutil.wait_procs(alive, timeout=timeout_seconds)
        return not parent.children(recursive=True)
    except Exception:
        return False


def build_kanban_stop_nudge(
    *,
    messages: Iterable[dict] | None = None,
    attempts: int = 0,
    max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
    task_id: Optional[str] = None,
) -> Optional[str]:
    """Return a synthetic follow-up when a kanban worker exits without a terminal tool.

    Returns ``None`` when the guard should not fire (not a kanban worker,
    already completed/blocked, or nudge budget exhausted).
    """
    if not kanban_stop_nudge_enabled():
        return None
    if attempts >= max_attempts:
        return None
    if session_called_kanban_terminal(messages):
        return None

    tid = (task_id or os.environ.get("HERMES_KANBAN_TASK") or "").strip() or "this task"
    return (
        "[System: You are a Hermes kanban worker. A plain-text reply is NOT a "
        "terminal state for the board.\n\n"
        f"Task `{tid}` is still `running`. Ending now without a board tool "
        "causes a protocol violation (clean exit with no "
        "`kanban_complete` / `kanban_block`).\n\n"
        "Do this immediately in your next response — do not narrate intent:\n"
        "1. Finish any remaining deliverable (write the required file(s) now).\n"
        "2. Call `kanban_complete(summary=..., artifacts=[...])` if the work "
        "is done, OR `kanban_block(reason=...)` if you are blocked.\n\n"
        "Never end a turn with only a promise of future action. Repeated "
        "protocol violations will block this task and require manual intervention.]"
    )


__all__ = [
    "build_kanban_stop_nudge",
    "kanban_stop_nudge_enabled",
    "reap_kanban_worker_descendants",
    "session_called_kanban_terminal",
    "session_succeeded_kanban_terminal",
]
