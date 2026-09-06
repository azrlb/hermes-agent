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


def _run_controlled_model(tmp_path, monkeypatch, *, kanban_worker=False, cli_completion=False, trailing_tool=False, receipt_command=""):
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
    artifact_relative = os.environ.get("HERMES_TEST_WORKER_ARTIFACT", "worker-evidence.md")
    artifact = tmp_path / artifact_relative
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
            assert len(calls) <= (5 if receipt_command else 4), "unexpected model retry"
            assert artifact.read_text(encoding="utf-8") == "verified fixture output\n"
            if len(calls) == 2:
                name, args = "terminal", {"command": f'git add "{artifact_relative}"'}
            elif len(calls) == 3:
                name, args = "terminal", {"command": 'git -c user.name="Worker Test" -c user.email=test@example.invalid commit -qm "worker evidence"'}
            elif receipt_command and len(calls) == 4:
                name, args = "terminal", {"command": "git push origin HEAD"}
            else:
                assert subprocess.check_output(['git', '-C', str(tmp_path), 'show', f'HEAD:{artifact_relative}'], text=True) == 'verified fixture output\n'
                if receipt_command:
                    name, args = "terminal", {"command": receipt_command}
                elif cli_completion:
                    name, args = "terminal", {"command": f'"{sys.executable}" -m hermes_cli.main kanban --board probe complete {os.environ["HERMES_KANBAN_TASK"]} --summary "Evidence saved in Git."'}
                else:
                    name, args = "kanban_complete", {"summary": "Evidence saved in Git."}
            tools = [SimpleNamespace(id=f"worker-step-{len(calls)}", type="function", function=SimpleNamespace(name=name, arguments=json.dumps(args)))]
            if trailing_tool and len(calls) == 4:
                tools.append(SimpleNamespace(id="forbidden-after-completion", type="function", function=SimpleNamespace(
                    name="write_file", arguments=json.dumps({"path": str(artifact), "content": "changed after completion\n"}))))
                if cli_completion:
                    # Two non-overlapping writes form a genuinely parallel
                    # segment after the terminal CLI barrier.
                    tools.append(SimpleNamespace(id="forbidden-parallel-write", type="function", function=SimpleNamespace(
                        name="write_file", arguments=json.dumps({"path": str(tmp_path / "forbidden.md"), "content": "must not exist\n"}))))
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
    assert len(calls) == (5 if receipt_command else 4 if kanban_worker else 2)
    assert artifact.read_text(encoding="utf-8") == "verified fixture output\n"
    assert not (tmp_path / "forbidden.md").exists()
    if trailing_tool:
        skipped = [message for message in agent._session_messages if message.get("role") == "tool"
                   and message.get("tool_call_id", "").startswith("forbidden-")]
        assert len(skipped) == (2 if cli_completion else 1)
        assert all("worker attempt is terminal" in message["content"] for message in skipped)


@pytest.mark.windows_only
@pytest.mark.parametrize("cli_completion", [False, True], ids=["model-tool", "receipt-cli-path"])
@pytest.mark.parametrize("trailing_tool", [False, True], ids=["single", "trailing-write"])
def test_supervised_agent_saves_git_output_and_fresh_observer_certifies_exit(tmp_path, cli_completion, trailing_tool, receipt_setup=None, assigned_worker=None):
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_worker_job import prepare_worker_command

    root = Path(__file__).resolve().parents[2]
    workspace = Path(assigned_worker["worktreePath"]) if assigned_worker else tmp_path / "worker"
    if not assigned_worker:
        workspace.mkdir()
        subprocess.run(["git", "init", "-q", str(workspace)], check=True, capture_output=True)
    environment = dict(os.environ, PYTHONPATH=str(root), HERMES_KANBAN_BOARD="probe")
    with kb.connect_closing(board="probe") as conn:
        tid = assigned_worker["hermesTaskId"] if assigned_worker else kb.create_task(conn, title="controlled agent process", assignee="probe", workspace_kind="dir", workspace_path=str(workspace))
        if assigned_worker:
            assigned = kb.get_task(conn, tid)
            assert assigned.status == "ready" and assigned.current_run_id is None
            resolved, branch = kb._resolve_worktree_workspace(assigned, board="probe")
            assert Path(resolved) == workspace
            assert branch == assigned_worker["expectedBranch"]
        code = """
import runpy, sys
from pathlib import Path
import pytest
from hermes_cli import kanban_db as kb
with pytest.MonkeyPatch.context() as isolated:
    runpy.run_path(sys.argv[1])['_run_controlled_model'](Path(sys.argv[2]), isolated, kanban_worker=True, cli_completion=sys.argv[3] == 'True', trailing_tool=sys.argv[4] == 'True', receipt_command=sys.argv[5])
print('WORKER_ASSERTIONS_PASSED', flush=True)
kb.write_kanban_worker_exit_record(0)
"""
        worker = None
        run_id = None

        def launch_controlled_model(task, workspace_path, board=None):
            nonlocal worker, run_id
            assert task.id == tid and Path(workspace_path) == workspace
            assert board == "probe" and worker is None
            run_id = task.current_run_id
            assert run_id is not None
            environment.update(HERMES_KANBAN_TASK=tid, HERMES_KANBAN_RUN_ID=str(run_id),
                               HERMES_KANBAN_EXIT_RECORD=str(kb._worker_exit_record_path(tid, run_id, board=board)))
            receipt_command = receipt_setup(tid, run_id, workspace, environment) if receipt_setup else ""
            command = prepare_worker_command(conn, tid, run_id,
                                             [sys.executable, "-u", "-c", code, str(Path(__file__).resolve()), str(workspace), str(cli_completion), str(trailing_tool), receipt_command])
            worker = subprocess.Popen(command, cwd=root, env=environment, stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE, text=True, creationflags=subprocess.CREATE_NO_WINDOW)
            return worker.pid

        try:
            if assigned_worker:
                from hermes_cli.profiles import get_profile_dir

                # Seed only the disposable assignment's profile. Do not bypass
                # the dispatcher's real profile eligibility check.
                profile = get_profile_dir(assigned.assignee)
                assert profile.is_relative_to(Path(os.environ["HERMES_HOME"]))
                profile.mkdir(parents=True, exist_ok=True)
                dispatched = kb.dispatch_once(conn, board="probe", max_spawn=1,
                                              spawn_fn=launch_controlled_model)
                assert [item[0] for item in dispatched.spawned] == [tid], dispatched
                assert kb.get_task(conn, tid).worker_pid == worker.pid
            else:
                task = kb.claim_task(conn, tid)
                kb._set_worker_pid(conn, tid, launch_controlled_model(task, str(workspace), board="probe"))
            assert worker is not None
            stdout, stderr = worker.communicate(timeout=75)
            assert worker.returncode == 0, (stdout, stderr)
            assert 'WORKER_ASSERTIONS_PASSED' in stdout, (stdout, stderr)
        finally:
            if worker is not None and worker.poll() is None:
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
    assert subprocess.check_output(['git', '-C', str(workspace), 'show', 'HEAD:' + environment.get('HERMES_TEST_WORKER_ARTIFACT', 'worker-evidence.md')], text=True) == 'verified fixture output\n'
