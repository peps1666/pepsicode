import test from 'node:test';
import assert from 'node:assert/strict';
import { startBackend } from '../src/backend.js';

test('external mode leaves ownership with the externally managed server', async () => {
  const backend = await startBackend({ backend: { mode: 'external', url: 'ws://127.0.0.1:12345', token: 'secret' } });
  assert.equal(backend.url, 'ws://127.0.0.1:12345');
  await backend.stop();
});

test('missing Python executable fails promptly without leaving a startup timer or child behind', async () => {
  await assert.rejects(startBackend({ backend: { mode: 'managed', python: 'pepsi-missing-python-test-executable' } }), /无法启动 Python/);
});

test('startup cancellation prevents spawning a managed child', async () => {
  await assert.rejects(startBackend({ backend: { mode: 'managed', python: process.execPath } },
    { signal: AbortSignal.abort() }), /abort/i);
});
