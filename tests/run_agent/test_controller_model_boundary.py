"""Controlled model output with the real agent loop and real file tool.

This is a building block for the controller lifecycle matrix, not process-exit
or controller-acceptance evidence. Only the model call is substituted.
"""

import json
import socket
from types import SimpleNamespace


def test_controlled_model_writes_real_worker_evidence(tmp_path, monkeypatch):
    def refuse_network(*_args, **_kwargs):
        raise OSError("network disabled for controlled model test")

    monkeypatch.setattr(socket.socket, "connect", refuse_network)
    from run_agent import AIAgent

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    artifact = tmp_path / "worker-evidence.md"
    agent = AIAgent(model="test-model", api_key="disposable-test-key",
                    base_url="http://127.0.0.1:1/v1", enabled_toolsets=["file"],
                    max_iterations=4, quiet_mode=True, skip_context_files=True,
                    skip_memory=True, platform="cli")
    agent._disable_streaming = True
    calls = []

    def model_response(payload):
        calls.append(payload)
        if len(calls) == 1:
            tools = [SimpleNamespace(id="write-evidence", type="function", function=SimpleNamespace(
                name="write_file", arguments=json.dumps({"path": str(artifact), "content": "verified fixture output\n"})))]
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
    assert result["final_response"] == "Evidence saved."
    assert len(calls) == 2
    assert artifact.read_text(encoding="utf-8") == "verified fixture output\n"
