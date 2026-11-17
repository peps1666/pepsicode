import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { randomBytes } from 'node:crypto';
import { setTimeout as delay } from 'node:timers/promises';
import { WebSocketServer } from 'ws';
import { RpcClient } from '../src/rpc.js';
import { StateStore } from '../src/store.js';
import { PrivateBridge } from '../src/bridge.js';

const OWNER = '123456789';
const BOT = '987654321';
let diagnose = () => '';
async function until(fn, details = () => diagnose()) {
  const deadline = Date.now() + 15000;
  while (!fn()) {
    if (Date.now() > deadline) throw new Error(`end-to-end condition timed out: ${details()}`);
    await delay(10);
  }
}

test('private QQ text traverses a real Python agent session, approvals, questions and restart recovery', { timeout: 60000 }, async (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'pepsi-qq-test-'));
  const token = randomBytes(32).toString('hex');
  const fixture = fileURLToPath(new URL('./backend_fixture.py', import.meta.url));
  const child = spawn(process.env.PEPSI_PYTHON ?? 'python', ['-u', fixture, root], {
    env: { ...process.env, PEPSI_CODE_SERVER_TOKEN: token, PYTHONIOENCODING: 'utf-8' }, windowsHide: true,
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  let output = '', errors = '';
  child.stderr.on('data', (chunk) => { errors = (errors + chunk.toString()).slice(-10000); });
  child.stdout.on('data', (chunk) => { output += chunk.toString(); });
  let childError;
  child.once('error', (error) => { childError = error; });
  const gateway = new WebSocketServer({ host: '127.0.0.1', port: 0 });
  const sent = [];
  const fatal = [];
  let qqSocket;
  let bridge, store;
  t.after(async () => {
    await bridge?.close();
    store?.close();
    for (const socket of gateway.clients) socket.terminate();
    await new Promise((resolve) => gateway.close(resolve));
    if (child.pid && child.exitCode === null) {
      await new Promise((resolve) => { child.once('exit', resolve); child.kill(); });
    }
    assert.equal(path.dirname(root), fs.realpathSync(os.tmpdir()));
    assert.ok(path.basename(root).startsWith('pepsi-qq-test-'));
    fs.rmSync(root, { recursive: true, force: true });
  });
  await new Promise((resolve) => gateway.once('listening', resolve));
  gateway.on('connection', (socket, request) => {
    assert.equal(request.headers.authorization, 'Bearer fixture-onebot-token');
    qqSocket = socket;
    socket.on('message', (raw) => {
      const packet = JSON.parse(raw.toString());
      let data;
      if (packet.action === 'get_login_info') data = { user_id: BOT, nickname: 'Pepsi' };
      if (packet.action === 'send_private_msg') {
        assert.equal(packet.params.user_id, Number(OWNER));
        sent.push(packet.params.message[0].data.text);
        data = { message_id: sent.length };
      }
      socket.send(JSON.stringify({ status: 'ok', retcode: 0, echo: packet.echo, data }));
    });
  });
  await until(() => childError || child.exitCode !== null || /listening on (ws:\/\/[^\s]+)/.test(output));
  assert.equal(childError, undefined);
  assert.equal(child.exitCode, null, errors);
  const url = output.match(/listening on (ws:\/\/[^\s]+)/)[1];
  const config = { ownerQQ: OWNER, botQQ: BOT, workspace: fs.realpathSync(root), stateDir: path.join(root, 'qq-state'),
    accessMode: 'auto', onebot: { url: `ws://127.0.0.1:${gateway.address().port}`, token: 'fixture-onebot-token' },
    limits: { queueSize: 16, inputChars: 12000, messageChars: 2000, sendDelayMs: 0, outboxSize: 200 } };
  const createBridge = async () => {
    store = new StateStore(config);
    bridge = new PrivateBridge({ config, store,
      pepsi: new RpcClient({ url, token, protocol: 'pepsi' }),
      onebot: new RpcClient({ ...config.onebot, protocol: 'onebot' }),
      log: () => {}, onFatal: (message) => fatal.push(message),
    });
    await bridge.start();
    await bridge.connectGateway();
  };
  await createBridge();
  diagnose = () => JSON.stringify({ sent, errors, fatal, state: store.data, busy: bridge.busy, paused: bridge.paused });
  let sequence = 100;
  const send = (text, extra = {}) => qqSocket.send(JSON.stringify({ post_type: 'message', message_type: 'private',
    sub_type: 'friend', user_id: Number(OWNER), self_id: Number(BOT), message_id: ++sequence,
    message: [{ type: 'text', data: { text } }], ...extra }));
  const has = (text) => sent.some((item) => item.includes(text));

  send('你好');
  await until(() => has('Pepsi：你好（第 1 条）') && !bridge.busy, () => JSON.stringify({ sent, errors, state: store.data }));
  send('记得我吗');
  await until(() => has('Pepsi：记得我吗（第 2 条）') && !bridge.busy);
  assert.equal(bridge.paused, false);
  assert.equal(store.data.lastStatus, 'completed');
  assert.equal(sent.some((text) => text.includes('没有通过完成验证')), false);
  const originalId = store.data.sessionId;
  const persisted = JSON.parse(fs.readFileSync(path.join(root, 'backend-state', 'sessions', `${originalId}.json`), 'utf8'));
  assert.equal(persisted.profile, 'qq_private');
  assert.ok(persisted.messages.some((message) => message.role === 'user' && message.content === '你好'));

  send('请确认操作');
  await until(() => bridge.approvals.size === 1, () => JSON.stringify({ sent, errors, state: store.data }));
  const prompt = [...bridge.approvals.keys()][0];
  assert.equal(has('Pepsi 工具结果：'), false);
  send(`/approve ${prompt}`, { user_id: 222222222 });
  await delay(50);
  assert.equal(bridge.approvals.size, 1);
  send(`/approve ${prompt}`);
  await until(() => has('Pepsi 工具结果：') && !bridge.busy);
  assert.ok(sent.some((text) => text.includes('Pepsi 工具结果：') && text.includes('QQ_APPROVED')), errors);

  send('问我问题');
  await until(() => has('你喜欢什么颜色？') && !bridge.busy);
  send('蓝色');
  await until(() => has('Pepsi：蓝色') && !bridge.busy);

  await bridge.close();
  store.close();
  await createBridge();
  assert.equal(store.data.sessionId, originalId);
  send('重启后继续');
  await until(() => has('Pepsi：重启后继续（第 6 条）') && !bridge.busy);
  assert.equal(bridge.paused, false);
  send('/new');
  await until(() => store.data.sessionId !== originalId);
  send('重新开始');
  await until(() => has('Pepsi：重新开始（第 1 条）') && !bridge.busy);
  assert.ok(fs.existsSync(path.join(root, 'backend-state', 'sessions', `${originalId}.json`)));
  assert.deepEqual(fatal, []);
});

test('a live WebSocket answer starts a replacement before the sibling Worker finishes', { timeout: 45000 }, async (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'pepsi-qq-orchestration-'));
  const token = randomBytes(32).toString('hex');
  const fixture = fileURLToPath(new URL('./backend_fixture.py', import.meta.url));
  const child = spawn(process.env.PEPSI_PYTHON ?? 'python', ['-u', fixture, root, 'orchestration'], {
    env: { ...process.env, PEPSI_CODE_SERVER_TOKEN: token, PYTHONIOENCODING: 'utf-8' },
    windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'],
  });
  let output = '', errors = '', spawnError, client;
  child.stdout.on('data', (chunk) => { output += chunk; });
  child.stderr.on('data', (chunk) => { errors += chunk; });
  child.once('error', (error) => { spawnError = error; });
  t.after(async () => {
    await client?.close();
    if (child.pid && child.exitCode === null && child.signalCode === null) {
      await new Promise((resolve) => { child.once('exit', resolve); child.kill(); });
    }
    assert.equal(path.dirname(root), fs.realpathSync(os.tmpdir()));
    assert.ok(path.basename(root).startsWith('pepsi-qq-orchestration-'));
    fs.rmSync(root, { recursive: true, force: true });
  });
  await until(() => spawnError || child.exitCode !== null || /listening on (ws:\/\/[^\s]+)/.test(output), () => errors);
  assert.ifError(spawnError);
  assert.equal(child.exitCode, null, errors);
  const url = output.match(/listening on (ws:\/\/[^\s]+)/)[1];
  client = new RpcClient({ url, token, protocol: 'pepsi' });
  await client.connect();
  const { session_id } = await client.request('session/create', { cwd: fs.realpathSync(root), profile: 'qq_private' });
  const turn = client.request('turn/run', { input: 'What is in this project?' }, { timeoutMs: 20000 });
  let complete = false;
  void turn.then(() => { complete = true; }, () => { complete = true; });

  let question, sibling;
  const deadline = Date.now() + 10000;
  while (!question && !complete && Date.now() < deadline) {
    const view = await client.request('agent/snapshot', { session_id });
    const asking = view.records.find((row) => row.can_reply_running);
    question = asking?.questions.find((item) => item.actionable);
    sibling = view.records.find((row) => row.task === 'Sibling investigation');
    if (!question) await delay(20);
  }
  assert.ok(question, errors);
  await until(() => fs.existsSync(path.join(root, 'coordinator-entered')), () => errors);
  assert.equal(sibling?.ended_at, null, errors);
  assert.equal(complete, false, errors);
  const receipt = await client.request('turn/enqueue', { input: 'Use settings.py', mode: 'steer', answer_to: [question.id] });
  fs.writeFileSync(path.join(root, 'reply-enqueued'), '');
  await turn;

  const saved = JSON.parse(fs.readFileSync(path.join(root, 'backend-state', 'sessions', `${session_id}.json`), 'utf8'));
  const control = saved.task_control;
  assert.equal(control.questions.find((item) => item.id === question.id).answer_input_id, receipt.id);
  const [original, other, replacement] = control.orchestration.work;
  assert.equal(other.state, 'stale');
  assert.ok(replacement, JSON.stringify({
    errors, toolResults: saved.messages.filter((item) => item.role === 'tool_result').slice(-3),
  }));
  assert.equal(replacement.retry_of, original.attempt_id);
  assert.equal(replacement.work_id, original.work_id);
  assert.equal(replacement.state, 'result_ready');
  assert.match(replacement.summary, /Resumed with settings\.py/);
});
