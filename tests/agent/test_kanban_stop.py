"""Tests for the kanban worker turn-end stop guard."""

from __future__ import annotations

import sys
import types

import pytest

from agent.kanban_stop import (
    build_kanban_stop_nudge,
    kanban_stop_nudge_enabled,
    reap_kanban_worker_descendants,
    session_called_kanban_terminal,
    session_succeeded_kanban_terminal,
)


@pytest.fixture
def clear_kanban_env(monkeypatch):
    for var in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_STOP_NUDGE"):
        monkeypatch.delenv(var, raising=False)
    return monkeypatch






def test_env_can_disable(clear_kanban_env):
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_abc")
    clear_kanban_env.setenv("HERMES_KANBAN_STOP_NUDGE", "0")
    assert kanban_stop_nudge_enabled() is False
    assert build_kanban_stop_nudge(messages=[]) is None


def test_nudge_when_no_terminal_tool(clear_kanban_env):
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_46be8aa5")
    messages = [
        {"role": "user", "content": "work kanban task"},
        {
            "role": "assistant",
            "content": "Let me write the comprehensive recipe.",
            "tool_calls": [
                {
                    "id": "1",
                    "type": "function",
                    "function": {"name": "kanban_heartbeat", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "name": "kanban_heartbeat", "tool_call_id": "1", "content": "ok"},
    ]
    nudge = build_kanban_stop_nudge(messages=messages, attempts=0)
    assert nudge is not None
    assert "kanban_complete" in nudge
    assert "kanban_block" in nudge
    assert "t_46be8aa5" in nudge
    assert "protocol violation" in nudge.lower() or "protocol" in nudge.lower()


def test_no_nudge_after_kanban_complete(clear_kanban_env):
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_abc")
    messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "1",
                    "type": "function",
                    "function": {"name": "kanban_complete", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "name": "kanban_complete", "tool_call_id": "1", "content": "done"},
    ]
    assert session_called_kanban_terminal(messages) is True
    assert build_kanban_stop_nudge(messages=messages) is None


def test_only_a_successful_terminal_result_requests_immediate_worker_exit():
    completed = [
        {"role": "assistant", "tool_calls": [{
            "id": "1", "type": "function",
            "function": {"name": "kanban_complete", "arguments": "{}"},
        }]},
        {"role": "tool", "name": "kanban_complete", "tool_call_id": "1", "content": '{"ok": true, "run_id": 7}'},
    ]
    rejected = [
        completed[0],
        {"role": "tool", "name": "kanban_complete", "tool_call_id": "1", "content": '{"error": "still running"}'},
    ]
    assert session_succeeded_kanban_terminal(completed) is True
    assert session_succeeded_kanban_terminal(rejected) is False


def test_terminal_worker_reaps_descendants_to_a_fixed_point(clear_kanban_env, monkeypatch):
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_abc")

    class Child:
        def __init__(self):
            self.terminated = False

        def terminate(self):
            self.terminated = True

        def kill(self):
            self.terminated = True

    child = Child()

    class Parent:
        def children(self, recursive=True):
            return [] if child.terminated else [child]

    fake = types.SimpleNamespace(
        Process=lambda pid: Parent(),
        NoSuchProcess=RuntimeError,
        AccessDenied=PermissionError,
        wait_procs=lambda children, timeout: (children, []),
    )
    monkeypatch.setitem(sys.modules, "psutil", fake)
    assert reap_kanban_worker_descendants(timeout_seconds=0) is True
    assert child.terminated is True


def test_terminal_worker_reports_surviving_descendant(clear_kanban_env, monkeypatch):
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_abc")

    class Child:
        def terminate(self):
            pass

        def kill(self):
            pass

    child = Child()

    class Parent:
        def children(self, recursive=True):
            return [child]

    fake = types.SimpleNamespace(
        Process=lambda pid: Parent(),
        NoSuchProcess=RuntimeError,
        AccessDenied=PermissionError,
        wait_procs=lambda children, timeout: ([], children),
    )
    monkeypatch.setitem(sys.modules, "psutil", fake)
    assert reap_kanban_worker_descendants(timeout_seconds=0) is False






# ── Integration: agent nudge + dispatcher bounded retry ──────────────
# These tests verify the two layers compose correctly: the agent-side
# nudge fires first (up to 2 attempts), and if the worker still exits
# without a terminal call, the dispatcher's bounded retry (streak of 3)
# handles it.  See also tests/hermes_cli/test_kanban_core_functionality.py
# for the dispatcher-side streak tests.


