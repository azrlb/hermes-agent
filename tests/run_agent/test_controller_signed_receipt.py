"""Joined worker/compiled receipt CLI/HTTP/Git/exit proof; not Restate acceptance."""

import hashlib
import hmac
import json
import os
from pathlib import Path
import runpy
import shutil
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest


@pytest.mark.windows_only
def test_real_worker_submits_signed_receipt_before_exact_attempt_completion(tmp_path):
    cli = os.environ.get("HERMES_TEST_CONTROLLER_RECEIPT_CLI")
    if not cli:
        pytest.skip("requires the compiled controller candidate HERMES_TEST_CONTROLLER_RECEIPT_CLI")
    assert Path(cli).is_file()
    node = shutil.which("node")
    assert node
    root = Path(__file__).resolve().parents[2]
    principal, secret = "hermes-technical-acceptor-v1", "disposable-receipt-secret"
    run_id, dispatch_id = "my244-signed-worker-fixture", "a" * 64
    received, failures = [], []

    class Receiver(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            try:
                if self.path == "/state":
                    response = {"request": {"runId": run_id}, "expectedEventSequence": 1,
                                "acceptedReceiptIds": [], "recoveryGeneration": 0}
                elif self.path == "/submit":
                    envelope = json.loads(body)
                    signature = envelope.pop("signature")
                    assert envelope["algorithm"] == "hmac-sha256"
                    assert envelope["keyId"] == principal
                    assert envelope["permissions"] == ["receipt"]
                    assert abs(time.time() - envelope["issuedAt"]) < 300
                    assert envelope["nonce"]
                    expected = hmac.new(secret.encode(), json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode(), hashlib.sha256).hexdigest()
                    assert hmac.compare_digest(signature, expected)
                    payload = envelope["payload"]
                    assert self.headers["Idempotency-Key"] == hashlib.sha256(json.dumps(payload, separators=(",", ":")).encode()).hexdigest()
                    assert payload["type"] == "receipt" and payload["sequence"] == 1
                    receipt = payload["receipt"]
                    assert receipt["runId"] == run_id
                    assert receipt["producer"]["dispatchId"] == dispatch_id
                    assert receipt["producer"]["principalId"] == principal
                    from hermes_cli import kanban_db as kb
                    with kb.connect_closing(board="probe") as conn:
                        task = kb.get_task(conn, receipt["producer"]["workerId"])
                        assert task.status == "running" and task.current_run_id is not None
                    received.append(receipt)
                    response = {"staged": True}
                else:
                    raise AssertionError("unexpected receiver endpoint")
                encoded = json.dumps(response).encode()
                self.send_response(200)
            except Exception as error:
                failures.append(repr(error))
                encoded = b'{}'
                self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Receiver)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def setup(tid, attempt, workspace, environment):
        remote = tmp_path / "origin.git"
        subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
        subprocess.run(["git", "-C", str(workspace), "checkout", "-qb", "codex/signed-fixture"], check=True)
        subprocess.run(["git", "-C", str(workspace), "remote", "add", "origin", str(remote)], check=True)
        key = tmp_path / "receipt.secret"
        key.write_text(secret, encoding="utf-8")
        base = f"http://127.0.0.1:{server.server_port}"
        context = {"request": {"runId": run_id, "dispatchId": dispatch_id,
            "eventSequence": 1, "phase": "technical-acceptance", "decision": "dispatch", "reasons": [],
            "linearIssueId": "MY-244", "githubRepository": "azrlb/accountible-app",
            "requirementsRevision": 1, "architectureRevision": 1, "modelSelection": {"model": "test-model"}},
            "principalId": principal, "role": "technical-acceptor", "workerId": tid,
            "expectedBranch": "codex/signed-fixture", "secretFile": str(key),
            "stateUrl": base + "/state", "submitUrl": base + "/submit", "hermesTaskId": tid, "hermesBoard": "probe"}
        context_path = tmp_path / "receipt-context.json"
        context_path.write_text(json.dumps(context), encoding="utf-8")
        environment["PATH"] = str(root / ".venv" / "Scripts") + os.pathsep + environment["PATH"]
        return f'"{node}" "{cli}" --context "{context_path}" --artifact worker-evidence.md'

    try:
        exercise = runpy.run_path(str(Path(__file__).with_name("test_controller_model_boundary.py")))["test_supervised_agent_saves_git_output_and_fresh_observer_certifies_exit"]
        exercise(tmp_path, cli_completion=True, trailing_tool=False, receipt_setup=setup)
        assert not failures, failures
        assert len(received) == 1
        receipt = received[0]
        blob = subprocess.check_output(["git", "--git-dir", str(tmp_path / "origin.git"), "show", f'{receipt["source"]["commit"]}:worker-evidence.md'])
        assert blob == b"verified fixture output\n"
        assert receipt["artifacts"][0]["sha256"] == hashlib.sha256(blob).hexdigest()
        assert receipt["checks"][0]["sha256"] == hashlib.sha256(blob).hexdigest()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
