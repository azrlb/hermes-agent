// Disposable worker payload: create real Git-bound evidence before process exit,
// without falsely claiming that the controller has received or accepted it.
const fs = require('node:fs');
const path = require('node:path');
const { randomUUID } = require('node:crypto');
const { execFileSync } = require('node:child_process');

(async () => {
  const [receiptCli, contextPath, artifactPath, outboxPath, python] = process.argv.slice(2);
  if (![receiptCli, contextPath, outboxPath, python].every(value => value && path.isAbsolute(value))) {
    throw new Error('disposable receipt preparation requires exact absolute paths');
  }
  const context = JSON.parse(fs.readFileSync(contextPath, 'utf8'));
  if (context.hermesBoard !== 'probe' || context.hermesTaskId !== process.env.HERMES_KANBAN_TASK) {
    throw new Error('receipt preparation must belong to the actual disposable worker');
  }
  const cli = require(receiptCli);
  const { createSignedEnvelope } = require(path.join(path.dirname(receiptCli), 'requestSigning.js'));
  const inputs = { artifactPath };
  const receipt = await cli.buildWorkerReceipt(context, inputs);
  const event = cli.workerControllerEvent(context, receipt, inputs);
  if (event.type !== 'receipt') throw new Error('exit-first fixture requires an actual passing receipt');
  const envelope = createSignedEnvelope(event, {
    keyId: context.principalId,
    secret: fs.readFileSync(context.secretFile, 'utf8').trim(),
    permissions: ['receipt'], issuedAt: Math.floor(Date.now() / 1000), nonce: randomUUID(),
  });
  fs.writeFileSync(outboxPath, JSON.stringify(envelope), { flag: 'wx' });
  // Terminal local work is distinct from controller acceptance. The parent
  // fixture must later prove a real exit, then deliver this exact receipt.
  execFileSync(python, ['-m', 'hermes_cli.main', 'kanban', '--board', 'probe',
    'complete', context.hermesTaskId, '--summary',
    'Disposable Git evidence signed and retained; controller delivery still pending.'],
  { windowsHide: true, stdio: 'pipe' });
})().catch(error => { console.error(error.message); process.exitCode = 1; });
