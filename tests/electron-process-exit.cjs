const { test } = require('node:test');
const assert = require('node:assert/strict');
const { EventEmitter } = require('node:events');
const { hasExited, waitForExit } = require('../electron/process-exit.cjs');

function processFixture() {
  return Object.assign(new EventEmitter(), { pid: 123, exitCode: null, signalCode: null });
}

test('already exited, signaled, absent and failed-to-spawn children need no grace wait', async () => {
  for (const child of [undefined, { pid: undefined, exitCode: null },
    { pid: 123, exitCode: 0, signalCode: null }, { pid: 123, exitCode: null, signalCode: 'SIGTERM' }]) {
    assert.equal(hasExited(child), true);
    assert.equal(await waitForExit(child, 60000), true);
  }
});

test('exit before the HTTP shutdown response is observed without a timeout or leaked listener', async () => {
  const child = processFixture();
  const exited = waitForExit(child, 60000);
  child.exitCode = 0;
  child.emit('exit', 0);
  await new Promise(resolve => setTimeout(resolve, 10)); // Response arrives after exit.
  assert.equal(await exited, true);
  assert.equal(child.listenerCount('exit'), 0);
});

test('exit after the response resolves normally and removes the timeout', async () => {
  const child = processFixture();
  const exited = waitForExit(child, 60000);
  setImmediate(() => { child.exitCode = 0; child.emit('exit', 0); });
  assert.equal(await exited, true);
  assert.equal(child.listenerCount('exit'), 0);
});

test('a still-running child exhausts the grace period without leaking an exit listener', async () => {
  const child = processFixture();
  assert.equal(await waitForExit(child, 20), false);
  assert.equal(hasExited(child), false);
  assert.equal(child.listenerCount('exit'), 0);
});
