"""Windows kanban process containment, established before worker code runs.

A named OS job tracks even grandchildren whose parents have already exited.
No breakaway is permitted. The supervisor retains the job handle until its
descendants exit and records that fact before exiting itself. Missing drain
evidence is not proof of exit. This module can run as a standalone bootstrap.
"""
from __future__ import annotations

import ctypes
import json
import os
from ctypes import wintypes
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
import uuid


def _kernel():
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    for name, args, result in (
        ("CreateJobObjectW", [ctypes.c_void_p, wintypes.LPCWSTR], wintypes.HANDLE),
        ("OpenJobObjectW", [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR], wintypes.HANDLE),
        ("GetCurrentProcess", [], wintypes.HANDLE),
        ("AssignProcessToJobObject", [wintypes.HANDLE, wintypes.HANDLE], wintypes.BOOL),
        ("TerminateJobObject", [wintypes.HANDLE, wintypes.UINT], wintypes.BOOL),
        ("SetInformationJobObject", [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                     wintypes.DWORD], wintypes.BOOL),
        ("QueryInformationJobObject", [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                       wintypes.DWORD, ctypes.c_void_p], wintypes.BOOL),
        ("CloseHandle", [wintypes.HANDLE], wintypes.BOOL),
    ):
        fn = getattr(api, name)
        fn.argtypes = args
        fn.restype = result
    return api


class _Accounting(ctypes.Structure):
    _fields_ = [
        ("user_time", ctypes.c_longlong), ("kernel_time", ctypes.c_longlong),
        ("period_user_time", ctypes.c_longlong), ("period_kernel_time", ctypes.c_longlong),
        ("page_faults", wintypes.DWORD), ("total_processes", wintypes.DWORD),
        ("active_processes", wintypes.DWORD), ("terminated_processes", wintypes.DWORD),
    ]


class _BasicLimits(ctypes.Structure):
    _fields_ = [
        ("process_time", ctypes.c_longlong), ("job_time", ctypes.c_longlong),
        ("flags", wintypes.DWORD), ("minimum_working_set", ctypes.c_size_t),
        ("maximum_working_set", ctypes.c_size_t), ("active_process_limit", wintypes.DWORD),
        ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD),
        ("scheduling_class", wintypes.DWORD),
    ]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [
        ("basic", _BasicLimits), ("io_counters", ctypes.c_ulonglong * 6),
        ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
        ("peak_process_memory", ctypes.c_size_t), ("peak_job_memory", ctypes.c_size_t),
    ]


def _set_kill_on_close(api, handle, enabled: bool) -> None:
    limits = _ExtendedLimits()
    limits.basic.flags = 0x2000 if enabled else 0  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not api.SetInformationJobObject(handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
        raise ctypes.WinError(ctypes.get_last_error())


def job_is_empty(name: str | None, attached: bool, drained: bool = False) -> bool:
    """Fail closed on missing evidence, access errors, or surviving members."""
    if not name or not attached or not drained:
        return False
    api = _kernel()
    handle = api.OpenJobObjectW(4, False, name)  # JOB_OBJECT_QUERY
    if not handle:
        # An absent name alone is NOT proof: the final user handle can close
        # before orphan members exit. Require the supervisor's durable drain.
        return ctypes.get_last_error() == 2  # ERROR_FILE_NOT_FOUND
    try:
        info = _Accounting()
        if not api.QueryInformationJobObject(handle, 1, ctypes.byref(info), ctypes.sizeof(info), None):
            return False
        return info.active_processes == 0
    finally:
        api.CloseHandle(handle)


def terminate_job(name: str | None, attached: bool, timeout: float = 5) -> bool:
    """Stop every member and retain the name until zero members is observed.

    An absent job is not proof of termination, including for legacy launches.
    This result is abnormal-stop evidence, never a successful worker result.
    """
    if not name or not attached:
        return False
    api = _kernel()
    handle = api.OpenJobObjectW(4 | 8, False, name)  # QUERY | TERMINATE
    if not handle:
        return False
    try:
        if not api.TerminateJobObject(handle, 1):
            return False
        deadline = time.monotonic() + timeout
        while True:
            info = _Accounting()
            if not api.QueryInformationJobObject(handle, 1, ctypes.byref(info), ctypes.sizeof(info), None):
                return False
            if info.active_processes == 0:
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.05)
    finally:
        api.CloseHandle(handle)


def prepare_worker_command(conn, task_id: str, run_id: int, command: list[str]) -> list[str]:
    """Bind a unique containment name to the exact claimed run before spawn."""
    name = "Local\\hermes-kanban-" + uuid.uuid4().hex
    database = conn.execute("PRAGMA database_list").fetchone()[2]
    with conn:
        changed = conn.execute(
            "UPDATE task_runs SET worker_job_name = ?, worker_job_attached = 0, worker_job_drained = 0, worker_job_exit_code = NULL "
            "WHERE id = ? AND task_id = ? AND ended_at IS NULL "
            "AND EXISTS (SELECT 1 FROM tasks WHERE id = ? AND current_run_id = ?)",
            (name, run_id, task_id, task_id, run_id),
        ).rowcount
        if changed != 1:
            raise RuntimeError("Cannot contain a worker without its exact active run")
    # The bootstrap uses only the standard library. Avoid the venv launcher
    # shim here so the returned PID belongs to the actual supervisor.
    return [sys._base_executable, str(Path(__file__).resolve()), database, task_id, str(run_id), name, *command]


def supervise(database: str, task_id: str, run_id: int, name: str, command: list[str]) -> int:
    api = _kernel()
    handle = api.CreateJobObjectW(None, name)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        if ctypes.get_last_error() == 183:  # name must belong to this launch alone
            raise RuntimeError("Worker containment name already exists")
        # A crashed supervisor must not leave its entire worker tree behind.
        # Establish the policy before assigning any process or launching code.
        _set_kill_on_close(api, handle, True)
        if not api.AssignProcessToJobObject(handle, api.GetCurrentProcess()):
            raise ctypes.WinError(ctypes.get_last_error())
        with sqlite3.connect(database, timeout=10) as conn:
            changed = conn.execute(
                "UPDATE task_runs SET worker_job_attached = 1 "
                "WHERE id = ? AND task_id = ? AND worker_job_name = ? AND ended_at IS NULL "
                "AND EXISTS (SELECT 1 FROM tasks WHERE id = ? AND current_run_id = ?)",
                (run_id, task_id, name, task_id, run_id),
            ).rowcount
            if changed != 1:
                raise RuntimeError("Worker run changed before containment attachment")
        # No worker code or subprocess can run before job assignment + commit.
        with subprocess.Popen(
            command, stdin=sys.stdin, stdout=sys.stdout, stderr=sys.stderr,
            creationflags=subprocess.CREATE_NO_WINDOW,
        ) as worker:
            result = worker.wait()
        # Stay alive (and retain the job handle) until all descendants exit.
        # Unlike parent-PID scans, accounting includes orphan grandchildren.
        while True:
            info = _Accounting()
            if not api.QueryInformationJobObject(handle, 1, ctypes.byref(info), ctypes.sizeof(info), None):
                raise ctypes.WinError(ctypes.get_last_error())
            if info.active_processes == 1:  # this supervisor alone
                break
            time.sleep(0.1)
        with sqlite3.connect(database, timeout=10) as conn:
            conn.execute(
                "UPDATE task_runs SET worker_job_drained = 1, worker_job_exit_code = ? "
                "WHERE id = ? AND task_id = ? AND worker_job_name = ? AND worker_job_attached = 1",
                (result, run_id, task_id, name),
            )
        # Preserve the installed capacity-wait restart contract, now bound to
        # the supervisor PID (the child interpreter has a different PID).
        record = os.environ.get("HERMES_KANBAN_EXIT_RECORD")
        if record and os.environ.get("HERMES_KANBAN_TASK") == task_id and os.environ.get("HERMES_KANBAN_RUN_ID") == str(run_id):
            target = Path(record)
            temporary = target.with_name(f"{target.name}.{os.getpid()}.tmp")
            temporary.write_text(json.dumps({
                "version": 1, "taskId": task_id, "runId": run_id,
                "pid": os.getpid(), "exitCode": result, "finishedAt": int(time.time()),
            }, sort_keys=True), encoding="utf-8")
            os.replace(temporary, target)
        # Only this supervisor remains, and its drain has been committed.
        # Avoid killing ourselves on a normal close and losing the true code.
        _set_kill_on_close(api, handle, False)
        return result
    finally:
        api.CloseHandle(handle)


if __name__ == "__main__":
    sys.exit(supervise(sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4], sys.argv[5:]))
