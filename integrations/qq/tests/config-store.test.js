import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { loadConfig, qqId } from '../src/config.js';
import { StateStore } from '../src/store.js';

function fixture(t) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'pepsi-qq-test-'));
  t.after(() => {
    assert.equal(path.dirname(root), fs.realpathSync(os.tmpdir()));
    assert.ok(path.basename(root).startsWith('pepsi-qq-test-'));
    fs.rmSync(root, { recursive: true, force: true });
  });
  const file = path.join(root, 'config.json');
  const raw = { ownerQQ: '123456789', botQQ: '987654321', workspace: '.',
    onebot: { wsUrl: 'ws://127.0.0.1:3001', tokenEnv: 'QQ_TEST_TOKEN' } };
  const write = () => fs.writeFileSync(file, `\uFEFF${JSON.stringify(raw)}`);
  write();
  const config = () => loadConfig(file, { QQ_TEST_TOKEN: 'test-token-private' });
  return { root, file, raw, write, config };
}

test('config resolves workspace against the config directory and requires an explicit owner/token', (t) => {
  const f = fixture(t);
  assert.equal(f.config().workspace, fs.realpathSync(f.root));
  assert.equal(f.config().accessMode, 'auto');
  assert.throws(() => loadConfig(f.file, {}), /QQ_TEST_TOKEN/);
  f.raw.ownerQQ = [];
  f.write();
  assert.throws(f.config, /ownerQQ/);
  assert.equal(qqId(Number.MAX_SAFE_INTEGER + 1), null);
  assert.equal(qqId('12345'), '12345');
});

test('config rejects remote endpoints, URL credentials, unsafe modes and unbounded limits', (t) => {
  const f = fixture(t);
  for (const url of ['wss://example.com', 'ws://user:secret@localhost:3001', 'ws://localhost:3001?token=secret', 'http://localhost']) {
    f.raw.onebot.wsUrl = url;
    f.write();
    assert.throws(f.config, /WebSocket|本机/);
  }
  f.raw.onebot.wsUrl = 'ws://localhost:3001';
  f.raw.accessMode = 'full_access';
  f.write();
  assert.throws(f.config, /accessMode/);
  delete f.raw.accessMode;
  f.raw.limits = { queueSize: 100000 };
  f.write();
  assert.throws(f.config, /queueSize/);
});

test('external backend requires its own token and accepts IPv6 loopback', (t) => {
  const f = fixture(t);
  f.raw.backend = { mode: 'external', url: 'ws://[::1]:8766', tokenEnv: 'PEPSI_TEST_TOKEN' };
  f.write();
  assert.throws(f.config, /PEPSI_TEST_TOKEN/);
  const config = loadConfig(f.file, { QQ_TEST_TOKEN: 'qq-secret', PEPSI_TEST_TOKEN: 'backend-secret' });
  assert.equal(config.backend.url, 'ws://[::1]:8766/');
});

test('store persists queues and dedupe, takes an exclusive lock, and marks in-progress sends uncertain', (t) => {
  const f = fixture(t);
  const config = f.config();
  const first = new StateStore(config);
  t.after(() => first.close());
  assert.throws(() => new StateStore(config), /重复启动/);
  first.data.inbox.push({ id: 'm1', text: 'hello' });
  first.data.recentIds.push('m1');
  first.data.outbox.push({ id: 'x1', text: 'reply', status: 'sending' });
  first.save();
  first.close();
  const second = new StateStore(config);
  assert.deepEqual(second.data.inbox, [{ id: 'm1', text: 'hello' }]);
  assert.equal(second.data.outbox[0].status, 'uncertain');
  assert.deepEqual(second.data.recentIds, ['m1']);
  second.close();
  assert.throws(() => new StateStore({ ...config, ownerQQ: '555555555' }), /不匹配/);
  assert.equal(fs.existsSync(path.join(config.stateDir, 'bridge.lock')), false);
});

test('corrupt state fails closed instead of discarding conversations or replaying tasks', (t) => {
  const f = fixture(t);
  const config = f.config();
  fs.mkdirSync(config.stateDir);
  fs.writeFileSync(path.join(config.stateDir, 'private.json'), '{"version":1');
  assert.throws(() => new StateStore(config), SyntaxError);
  assert.equal(fs.existsSync(path.join(config.stateDir, 'bridge.lock')), false);
});
