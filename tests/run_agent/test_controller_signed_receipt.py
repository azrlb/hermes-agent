"""Joined worker/compiled receipt CLI/HTTP/Git/exit proof; not Restate acceptance."""

import hashlib
import hmac
import json
import os
from pathlib import Path
import runpy
import shutil
import subprocess
import sys
import threading
import time
from urllib.request import Request, urlopen
from urllib.parse import urlparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest


@pytest.mark.windows_only
def test_real_worker_submits_signed_receipt_before_exact_attempt_completion(tmp_path, tmp_path_factory, request):
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
    upstream = {}
    active_task_id = None
    setup_url = os.environ.get("HERMES_TEST_CONTROLLER_SETUP_URL")
    completed = False
    def dispose_failed_fixture():
        if completed or not setup_url:
            return
        endpoint = setup_url.rsplit('/', 1)[0] + '/dispose'
        result = post(endpoint, {})
        deadline = time.monotonic() + 90
        while result['status'] == 'pending' and time.monotonic() < deadline:
            time.sleep(0.25)
            result = post(endpoint + '-result', {})
        assert result['status'] == 'verified', result
    # Runs before tmp_path/environment teardown, including setup exceptions.
    request.addfinalizer(dispose_failed_fixture)
    if setup_url:
        assert urlparse(setup_url).hostname == "127.0.0.1", "controller harness must be disposable loopback"
        if setup_url.endswith('/assignment'):
            # The real linked checkout nests both Git metadata and receipt
            # artifacts. Keep the disposable root short on Windows.
            tmp_path = tmp_path_factory.mktemp('g')

    def post(url, payload):
        request = Request(url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
        with urlopen(request, timeout=30) as response:
            return json.load(response)

    class Receiver(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            try:
                if self.path == "/state" or self.path.endswith('/state'):
                    response = post(upstream["stateUrl"], {}) if upstream else {
                        "request": {"runId": run_id}, "expectedEventSequence": 1,
                        "acceptedReceiptIds": [], "recoveryGeneration": 0}
                elif self.path == "/submit" or self.path.endswith('/submitEvent'):
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
                    # Recovery consumes an operator event before this worker.
                    # Validate its exact immutable dispatch sequence, not an
                    # assumed first event or whatever the current state says.
                    assert payload["type"] == "receipt"
                    assert payload["sequence"] == upstream.get("eventSequence", 1)
                    receipt = payload["receipt"]
                    assert receipt["runId"] == run_id
                    assert receipt["producer"]["dispatchId"] == dispatch_id
                    assert receipt["producer"]["principalId"] == principal
                    from hermes_cli import kanban_db as kb
                    with kb.connect_closing(board="probe") as conn:
                        task = kb.get_task(conn, active_task_id)
                        assert task.status == "running" and task.current_run_id is not None
                    received.append(receipt)
                    response = post(upstream["submitUrl"], json.loads(body)) if upstream else {"staged": True}
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
    assigned_worker = None
    if setup_url and setup_url.endswith('/assignment'):
        repository = tmp_path / 'r'
        (repository / 'packages/shared').mkdir(parents=True)
        manifest = {'name': 'worker-fixture', 'version': '1.0.0', 'workspaces': ['packages/shared']}
        child = {'name': '@fixture/shared', 'version': '1.0.0'}
        (repository / 'package.json').write_text(json.dumps(manifest), encoding='utf-8')
        (repository / 'packages/shared/package.json').write_text(json.dumps(child), encoding='utf-8')
        (repository / 'package-lock.json').write_text(json.dumps({'lockfileVersion': 3, 'packages': {
            '': manifest, 'packages/shared': child, 'node_modules/@fixture/shared': {'resolved': 'packages/shared', 'link': True}}}), encoding='utf-8')
        remote = tmp_path / 'origin.git'
        subprocess.run(['git', 'init', '--bare', '-q', str(remote)], check=True)
        subprocess.run(['git', 'init', '-q', str(repository)], check=True)
        subprocess.run(['git', '-C', str(repository), 'remote', 'add', 'origin', str(remote)], check=True)
        subprocess.run(['git', '-C', str(repository), 'add', 'package.json', 'package-lock.json', 'packages/shared/package.json'], check=True)
        subprocess.run(['git', '-C', str(repository), '-c', 'user.name=Fixture', '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'gateway input'], check=True)
        commit = subprocess.check_output(['git', '-C', str(repository), 'rev-parse', 'HEAD'], text=True).strip()
        assigned_worker = post(setup_url, {'workspace': str(repository), 'home': os.environ['HERMES_HOME'], 'origin': str(remote), 'baseCommit': commit,
            'receiptBase': f'http://127.0.0.1:{server.server_port}'})
        if assigned_worker.get('pending'):
            deadline = time.monotonic() + 90
            while assigned_worker.get('pending') and time.monotonic() < deadline:
                time.sleep(0.25)
                assigned_worker = post(setup_url + '-result', {})
            assert 'assignment' in assigned_worker, 'the same controller setup operation did not finish'
            assigned_worker = assigned_worker['assignment']

    def setup(tid, attempt, workspace, environment):
        nonlocal principal, run_id, dispatch_id, active_task_id
        active_task_id = tid
        remote = tmp_path / "origin.git"
        if not assigned_worker:
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
        if setup_url and not assigned_worker:
            seed = workspace / "seed.md"
            seed.write_text("disposable initial state\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(workspace), "add", "seed.md"], check=True)
            subprocess.run(["git", "-C", str(workspace), "-c", "user.name=Fixture", "-c", "user.email=test@example.invalid", "commit", "-qm", "initial fixture"], check=True)
            commit = subprocess.check_output(["git", "-C", str(workspace), "rev-parse", "HEAD"], text=True).strip()
            supplied = post(setup_url, {"taskId": tid, "attempt": attempt, "workspace": str(workspace),
                "home": environment["HERMES_HOME"], "origin": str(remote), "baseCommit": commit})
            if supplied.get('pending'):
                deadline = time.monotonic() + 90
                while supplied.get('pending') and time.monotonic() < deadline:
                    time.sleep(0.25)
                    supplied = post(setup_url + '-result', {})
                assert 'assignment' in supplied, 'the same controller setup operation did not finish'
                supplied = supplied['assignment']
        if setup_url:
            if assigned_worker:
                supplied = assigned_worker
                assert tid == supplied['hermesTaskId']
            upstream.update(stateUrl=supplied.get('nativeStateUrl', supplied['stateUrl']), submitUrl=supplied.get('nativeSubmitUrl', supplied['submitUrl']),
                            eventSequence=supplied['request']['eventSequence'])
            context.update(supplied)
            context.update(stateUrl=base + "/state", submitUrl=base + "/submit", secretFile=str(key))
            principal = context["principalId"]
            run_id, dispatch_id = context["request"]["runId"], context["request"]["dispatchId"]
            environment["HERMES_TEST_WORKER_ARTIFACT"] = f"_bmad-output/orchestrator-runs/{run_id}/worker-evidence.md"
            if assigned_worker and assigned_worker.get('repairReview'):
                # Each real worker saves separate Git evidence; the reviewer
                # must not produce an empty commit by rewriting the repair file.
                environment["HERMES_TEST_WORKER_ARTIFACT"] = f"_bmad-output/orchestrator-runs/{run_id}/{dispatch_id}.md"
        if assigned_worker:
            context_path = Path(assigned_worker['contextFile'])
            generated = json.loads(context_path.read_text(encoding='utf-8'))
            assert generated['request'] == assigned_worker['request']
            assert generated['hermesTaskId'] == tid
            Path(generated['secretFile']).write_text(secret, encoding='utf-8')
        else:
            context_path = tmp_path / "receipt-context.json"
            context_path.write_text(json.dumps(context), encoding="utf-8")
        environment["PATH"] = str(root / ".venv" / "Scripts") + os.pathsep + environment["PATH"]
        if assigned_worker and assigned_worker.get('exitFirst'):
            outbox = tmp_path / 'exit-first-envelope.json'
            helper = Path(__file__).with_name('prepare_exit_first_receipt.cjs')
            # The terminal tool uses a shell; list2cmdline targets CreateProcess
            # and leaves no-space Windows paths unquoted, losing backslashes.
            arguments = [node, str(helper), cli, str(context_path),
                environment['HERMES_TEST_WORKER_ARTIFACT'], str(outbox), sys.executable]
            return ' '.join(json.dumps(argument.replace('\\', '/')) for argument in arguments)
        return f'"{node}" "{cli}" --context "{context_path}" --artifact "{environment.get("HERMES_TEST_WORKER_ARTIFACT", "worker-evidence.md")}"'

    try:
        if assigned_worker and assigned_worker.get('controlMode') in ('cancel', 'pause', 'revise'):
            exercise_busy = runpy.run_path(str(Path(__file__).with_name('controller_busy_worker_fixture.py')))['exercise_busy_worker_cancel']
            exercise_busy(assigned_worker, setup_url.rsplit('/', 1)[0] + '/control', post)
            assert received == [] and failures == []
            completed = True
            return
        exercise = runpy.run_path(str(Path(__file__).with_name("test_controller_model_boundary.py")))["test_supervised_agent_saves_git_output_and_fresh_observer_certifies_exit"]
        exercise(tmp_path, cli_completion=True, trailing_tool=False, receipt_setup=setup, assigned_worker=assigned_worker)
        if assigned_worker and assigned_worker.get('exitFirst'):
            assert received == [] and failures == [], 'receipt reached controller before real exit'
            envelope = json.loads((tmp_path / 'exit-first-envelope.json').read_text())
            unsigned = {key: value for key, value in envelope.items() if key != 'signature'}
            expected = hmac.new(secret.encode(), json.dumps(unsigned, sort_keys=True, separators=(',', ':')).encode(), hashlib.sha256).hexdigest()
            assert hmac.compare_digest(envelope['signature'], expected)
            assert envelope['keyId'] == principal and envelope['payload']['receipt']['producer']['dispatchId'] == dispatch_id
            result = post(setup_url.rsplit('/', 1)[0] + '/exit-first-ready', {'envelope': envelope})
            assert result['realExitStagedFirst'] is True
            received.append(envelope['payload']['receipt'])
        assert not failures, failures
        assert len(received) == 1
        receipt = received[0]
        artifact_path = receipt["artifacts"][0]["uri"].split('/blob/' + receipt["source"]["commit"] + '/', 1)[1]
        blob = subprocess.check_output(["git", "--git-dir", str(tmp_path / "origin.git"), "show", f'{receipt["source"]["commit"]}:{artifact_path}'])
        assert blob == b"verified fixture output\n"
        assert receipt["artifacts"][0]["sha256"] == hashlib.sha256(blob).hexdigest()
        assert receipt["checks"][0]["sha256"] == hashlib.sha256(blob).hexdigest()
        if assigned_worker and assigned_worker.get('repairReview'):
            repair_assignment = assigned_worker
            repair_receipt = receipt
            endpoint = setup_url.rsplit('/', 1)[0] + '/next-review'
            result = post(endpoint, {})
            deadline = time.monotonic() + 90
            while result.get('pending') and time.monotonic() < deadline:
                time.sleep(0.25)
                result = post(endpoint, {})
            assert 'assignment' in result, result
            assigned_worker = result['assignment']
            assert assigned_worker['principalId'] != repair_assignment['principalId']
            assert assigned_worker['hermesTaskId'] != repair_assignment['hermesTaskId']
            assert assigned_worker['request']['baseCommit'] == repair_receipt['source']['commit']
            # Run another genuine supervised worker through the generated
            # assignment, receipt CLI, receiver and fresh exit observer.
            exercise(tmp_path, cli_completion=True, trailing_tool=False, receipt_setup=setup, assigned_worker=assigned_worker)
            assert failures == [] and len(received) == 2, failures
            receipt = received[1]
            assert receipt['producer']['principalId'] == assigned_worker['principalId']
            repair_blob = subprocess.check_output(['git', '--git-dir', str(tmp_path / 'origin.git'), 'show',
                f"{receipt['source']['commit']}:{artifact_path}"])
            assert repair_blob == blob, 'independent review lost the saved repair evidence'
        if assigned_worker and assigned_worker.get("callbackLoss"):
            from hermes_cli import kanban_db as kb
            dropped = []
            def drop_exit_delivery(url, payload):
                if payload.get("payload", {}).get("type") == "worker-exited":
                    dropped.append(payload)
                    raise ConnectionError("disposable injected callback loss before delivery")
                return post(url, payload)
            with kb.connect_closing(board="probe") as conn:
                before = dict(conn.execute("SELECT * FROM task_runs WHERE task_id=? ORDER BY id DESC LIMIT 1", (active_task_id,)).fetchone())
                assert before["worker_exited_at"] and before["worker_exit_code"] == 0
                assert kb.deliver_worker_exit_certificates(conn, board="probe", key_id="hermes-lifecycle-v1",
                    secret="disposable-lifecycle-secret", transport=drop_exit_delivery) == []
                after = dict(conn.execute("SELECT * FROM task_runs WHERE id=?", (before["id"],)).fetchone())
                assert len(dropped) == 1
                assert after["worker_exit_delivery_attempts"] == before["worker_exit_delivery_attempts"] + 1
                assert after["worker_exit_delivered_at"] is None
                for field in ("worker_pid", "process_started_at", "worker_exited_at", "worker_exit_code", "worker_exit_kind", "outcome"):
                    assert after[field] == before[field]
            result = post(setup_url.rsplit("/", 1)[0] + "/callback-loss-recorded", {})
            assert result == {"pollingReleased": True}
        if setup_url:
            # Keep the disposable board and Git remote alive until the real
            # controller has independently consumed both pieces of evidence.
            endpoint = setup_url.rsplit("/", 1)[0] + "/finish"
            result = post(endpoint, {"receiptId": receipt["receiptId"]})
            deadline = time.monotonic() + 90
            while result.get("status") == "pending" and time.monotonic() < deadline:
                time.sleep(0.25)
                result = post(endpoint + "-result", {})
            assert result.get("status") == "verified", result
            assert result["acceptedReceiptIds"] == [item["receiptId"] for item in received], result
        completed = True
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
