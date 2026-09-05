"""Controlled model output with the real agent loop and real file tool.

The supervised case also exercises real Git tools, terminal task completion,
and fresh-process exit certification. Controller acceptance is not covered.
Only the model call is substituted.
"""

import os
import json
import socket
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


def test_controlled_model_writes_real_worker_evidence(tmp_path, monkeypatch):
    _run_controlled_model(tmp_path, monkeypatch)


def _run_controlled_model(tmp_path, monkeypatch, *, kanban_worker=False):
    original_connect = socket.socket.connect

    def refuse_network(sock, address):
        if isinstance(address, tuple) and address[0] in ("127.0.0.1", "::1"):
            return original_connect(sock, address)
        raise OSError("network disabled for controlled model test")

    monkeypatch.setattr(socket.socket, "connect", refuse_network)
    from run_agent import AIAgent

    if not kanban_worker:
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    artifact = tmp_path / "worker-evidence.md"
    agent = AIAgent(model="test-model", api_key="disposable-test-key",
                    base_url="http://127.0.0.1:1/v1", enabled_toolsets=["file", "terminal", "kanban"] if kanban_worker else ["file"],
                    max_iterations=6, quiet_mode=True, skip_context_files=True,
                    skip_memory=True, platform="cli")
    agent._disable_streaming = True
    calls = []

    def model_response(payload):
        calls.append(payload)
        if len(calls) == 1:
            tools = [SimpleNamespace(id="write-evidence", type="function", function=SimpleNamespace(
                name="write_file", arguments=json.dumps({"path": str(artifact), "content": "verified fixture output\n"})))]
            content = None
        elif kanban_worker:
            assert len(calls) <= 4, "unexpected model retry"
            assert artifact.read_text(encoding="utf-8") == "verified fixture output\n"
            if len(calls) == 2:
                name, args = "terminal", {"command": "git add worker-evidence.md"}
            elif len(calls) == 3:
                name, args = "terminal", {"command": 'git -c user.name="Worker Test" -c user.email=test@example.invalid commit -qm "worker evidence"'}
            else:
                assert subprocess.check_output(['git', '-C', str(tmp_path), 'show', 'HEAD:worker-evidence.md'], text=True) == 'verified fixture output\n'
                name, args = "kanban_complete", {"summary": "Evidence saved in Git."}
            tools = [SimpleNamespace(id=f"worker-step-{len(calls)}", type="function", function=SimpleNamespace(name=name, arguments=json.dumps(args)))]
            content = None
        else:
            assert len(calls) == 2, "unexpected model retry"
            assert artifact.read_text(encoding="utf-8") == "verified fixture output\n"
            assert any(message.get("role") == "tool" for message in payload["messages"])
            tools, content = None, "Evidence saved."
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
            content=content, tool_calls=tools, reasoning=None, reasoning_content=None, reasoning_details=None),
            finish_reason="tool_calls" if tools else "stop")], usage=None, model="test-model")

    monkeypatch.setattr(agent, "_interruptible_api_call", model_response)
    result = agent.run_conversation("Save the disposable worker evidence, then finish.")
    assert not result.get("failed"), result
    if kanban_worker:
        assert result["turn_exit_reason"] == "kanban_terminal_succeeded", result
    else:
        assert result["final_response"] == "Evidence saved."
    assert len(calls) == (4 if kanban_worker else 2)
    assert artifact.read_text(encoding="utf-8") == "verified fixture output\n"


@pytest.mark.windows_only
def test_supervised_agent_saves_git_output_and_fresh_observer_certifies_exit(tmp_path):
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_worker_job import prepare_worker_command

    root = Path(__file__).resolve().parents[2]
    workspace = tmp_path / "worker"
    workspace.mkdir()
    subprocess.run(["git", "init", "-q", str(workspace)], check=True, capture_output=True)
    environment = dict(os.environ, PYTHONPATH=str(root), HERMES_KANBAN_BOARD="probe")
    with kb.connect_closing(board="probe") as conn:
        tid = kb.create_task(conn, title="controlled agent process", assignee="probe", workspace_kind="dir", workspace_path=str(workspace))
        task = kb.claim_task(conn, tid)
        run_id = task.current_run_id
        environment.update(HERMES_KANBAN_TASK=tid, HERMES_KANBAN_RUN_ID=str(run_id),
                           HERMES_KANBAN_EXIT_RECORD=str(kb._worker_exit_record_path(tid, run_id, board="probe")))
        code = """
import runpy, sys
from pathlib import Path
import pytest
from hermes_cli import kanban_db as kb
with pytest.MonkeyPatch.context() as isolated:
    runpy.run_path(sys.argv[1])['_run_controlled_model'](Path(sys.argv[2]), isolated, kanban_worker=True)
print('WORKER_ASSERTIONS_PASSED', flush=True)
kb.write_kanban_worker_exit_record(0)
"""
        command = prepare_worker_command(conn, tid, run_id,
                                         [sys.executable, "-u", "-c", code, str(Path(__file__).resolve()), str(workspace)])
        worker = subprocess.Popen(command, cwd=root, env=environment, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True, creationflags=subprocess.CREATE_NO_WINDOW)
        kb._set_worker_pid(conn, tid, worker.pid)
        try:
            stdout, stderr = worker.communicate(timeout=75)
            assert worker.returncode == 0, (stdout, stderr)
            assert 'WORKER_ASSERTIONS_PASSED' in stdout, (stdout, stderr)
        finally:
            if worker.poll() is None:
                kb.stop_task(conn, tid, reason="dispose controlled model test")
                worker.communicate(timeout=20)
    # This process did not own the Popen handle or its in-memory exit cache.
    observer = """
import json,sys
from dataclasses import asdict
from hermes_cli import kanban_db as kb
with kb.connect_closing(board='probe') as conn:
    kb.certify_terminal_worker_exits(conn)
    print(json.dumps(asdict(kb.get_run(conn,int(sys.argv[1])))))
"""
    observed = json.loads(subprocess.check_output([sys.executable, "-c", observer, str(run_id)],
                                                cwd=root, env=environment, text=True, timeout=30))
    assert observed['worker_exit_code'] == 0, (json.dumps(observed, sort_keys=True) + '\n' + stdout)
    assert observed['worker_exit_kind'] == 'clean_exit'
    assert observed['worker_pid'] == worker.pid
    assert observed['worker_exited_at'] >= observed['process_started_at']
    assert subprocess.check_output(['git', '-C', str(workspace), 'show', 'HEAD:worker-evidence.md'], text=True) == 'verified fixture output\n'
