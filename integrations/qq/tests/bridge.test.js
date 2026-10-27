import test from 'node:test';
import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import { setTimeout as delay } from 'node:timers/promises';
import { PrivateBridge, privateText } from '../src/bridge.js';

const OWNER = '123456789';
const BOT = '987654321';
function message(text, id = 1, extra = {}) {
  return { post_type: 'message', message_type: 'private', sub_type: 'friend',
    self_id: Number(BOT), user_id: Number(OWNER), message_id: id,
    message: [{ type: 'text', data: { text } }], ...extra };
}
async function until(fn) {
  const deadline = Date.now() + 2000;
  while (!fn()) {
    if (Date.now() > deadline) throw new Error('test condition timed out');
    await delay(2);
  }
}
class Client extends EventEmitter {
  constructor(protocol) {
    super();
    this.protocol = protocol;
    this.token = protocol === 'pepsi' ? 'pepsi-secret-token' : 'qq-secret-token';
    this.connected = false;
    this.calls = [];
    this.turns = [];
    this.sequence = 0;
    this.sent = [];
  }
  async connect() { this.connected = true; }
  async close() { this.connected = false; }
  async request(method, params = {}, options = {}) {
    this.calls.push({ method, params, options });
    if (method === 'get_login_info') return { user_id: BOT };
    if (method === 'send_private_msg') {
      if (this.failSend) { this.failSend = false; throw new Error('timeout'); }
      this.sent.push(params);
      return { message_id: this.sent.length };
    }
    if (method === 'session/create') return { session_id: (++this.sequence).toString(16).padStart(12, '0'), profile: 'qq_private' };
    if (method === 'session/resume') return { profile: 'qq_private', ...this.restored };
    if (method === 'tool/approve') return { resolved: true };
    if (method === 'turn/run') return new Promise((resolve, reject) => { this.turns.push({ params, resolve, reject }); });
    return {};
  }
  event(event, data) { this.emit('notification', { kind: 'event', event, data }); }
  finish(text = '你好，我是 Pepsi') {
    this.event('message/delta', { text: 'ignored streaming' });
    this.event('message/end', { content: text });
    this.event('turn/end', { status: 'completed' });
    this.turns.at(-1).resolve({ last_message: text });
  }
}
async function fixture(t, seed = {}, restored = {}) {
  const config = { ownerQQ: OWNER, botQQ: BOT, workspace: '/project', accessMode: 'auto',
    onebot: { token: 'qq-secret-token' }, limits: { queueSize: 2, inputChars: 1000, messageChars: 2000, sendDelayMs: 0, outboxSize: 100 } };
  const store = { data: { version: 1, sessionId: null, inbox: [], outbox: [], recentIds: [], inFlight: null, ...seed }, saves: [],
    save() { this.saves.push(structuredClone(this.data)); } };
  const pepsi = new Client('pepsi');
  pepsi.restored = restored;
  const onebot = new Client('onebot');
  const fatal = [];
  const bridge = new PrivateBridge({ config, store, pepsi, onebot, log: () => {}, onFatal: (text) => fatal.push(text) });
  await bridge.start();
  await bridge.connectGateway();
  await until(() => !bridge.flushing);
  onebot.sent = [];
  t.after(async () => {
    for (const turn of pepsi.turns) turn.resolve({});
    await bridge.close();
  });
  return { config, store, pepsi, onebot, bridge, fatal, texts: () => onebot.sent.map((item) => item.message[0].data.text) };
}

test('only owner friend text on the bound QQ account enters the conversation', () => {
  assert.equal(privateText(message('hi'), OWNER, BOT), 'hi');
  for (const extra of [
    { user_id: 222222222 }, { user_id: Number(BOT) }, { self_id: 333333333 },
    { message_type: 'group' }, { sub_type: 'group' }, { group_id: 123 }, { post_type: 'message_sent' },
  ]) assert.equal(privateText(message('hi', 1, extra), OWNER, BOT), null);
  assert.equal(privateText(message('', 1, { message: [{ type: 'image', data: { url: 'file:///secret' } }] }), OWNER, BOT), '');
  assert.equal(privateText(message('', 1, { message: '[CQ:image,file=x]look' }), OWNER, BOT), '');
  assert.equal(privateText(message('', 1, { message: '&#91;CQ:image,file=x&#93;' }), OWNER, BOT), '[CQ:image,file=x]');
});

test('deduplicates incoming messages durably and replies once without progress/thinking/tool logs', async (t) => {
  const f = await fixture(t);
  await f.bridge.receive(message('你好'));
  await f.bridge.receive(message('你好'));
  assert.equal(f.pepsi.turns.length, 1);
  assert.equal(f.store.saves.some((s) => s.inFlight?.text === '你好' && s.recentIds.includes(`${BOT}:1`)), true);
  f.pepsi.event('thinking/delta', { text: 'hidden' });
  f.pepsi.event('tool/result', { tool: 'read_file', output: 'hidden file' });
  f.pepsi.event('message/end', { role: 'progress', content: 'hidden progress' });
  f.pepsi.finish('**你好**，很高兴见到你');
  await f.bridge.pumping;
  await f.bridge.flush();
  assert.deepEqual(f.texts(), ['你好，很高兴见到你']);
  assert.equal(f.onebot.sent[0].user_id, Number(OWNER));
  assert.equal(f.store.data.inFlight, null);
});

test('queues new messages in order, bounds admission, and stop clears only pending work', async (t) => {
  const f = await fixture(t);
  await f.bridge.receive(message('one', 1));
  await f.bridge.receive(message('two', 2));
  await f.bridge.receive(message('three', 3));
  await f.bridge.receive(message('four', 4));
  assert.deepEqual(f.store.data.inbox.map((m) => m.text), ['two', 'three']);
  f.pepsi.finish('one reply');
  await until(() => f.pepsi.turns.length === 2);
  assert.equal(f.pepsi.turns[1].params.input, 'two');
  await f.bridge.receive(message('/stop', 5));
  assert.equal(f.store.data.inbox.length, 0);
  assert.equal(f.pepsi.calls.some((call) => call.method === 'turn/cancel'), true);
  f.pepsi.finish('stopped');
  await f.bridge.pumping;
  assert.equal(f.pepsi.turns.length, 2);
  assert.equal(f.bridge.paused, true);
});

test('approvals require owner, exact live prompt ID, and grant only allow_once', async (t) => {
  const f = await fixture(t);
  await f.bridge.receive(message('run a task'));
  f.pepsi.event('permission/request', { prompt_id: 'abc123', summary: 'run command', details: ['echo hi'] });
  await f.bridge.receive(message('/approve abc123', 2, { user_id: 222222222 }));
  await f.bridge.receive(message('/approve wrong', 3));
  assert.equal(f.pepsi.calls.filter((call) => call.method === 'tool/approve').length, 0);
  await f.bridge.receive(message('/approve abc123', 4));
  assert.deepEqual(f.pepsi.calls.filter((call) => call.method === 'tool/approve').map((call) => call.params),
    [{ prompt_id: 'abc123', decision: 'allow_once' }]);
  await f.bridge.receive(message('/approve abc123', 5));
  assert.equal(f.pepsi.calls.filter((call) => call.method === 'tool/approve').length, 1);
  f.pepsi.event('permission/request', { prompt_id: 'expired', details: [] });
  f.bridge.approvals.set('expired', Date.now() - 1);
  await f.bridge.receive(message('/deny expired', 6));
  assert.equal(f.pepsi.calls.filter((call) => call.method === 'tool/approve').length, 1);
  f.pepsi.finish();
  await f.bridge.pumping;
});

test('normal text answers an ask_user question without requiring a continue command', async (t) => {
  const f = await fixture(t);
  await f.bridge.receive(message('帮我写点东西', 1));
  f.pepsi.event('tool/result', { tool: 'ask_user', output: '写什么？' });
  f.pepsi.event('message/end', { content: '写什么？' });
  f.pepsi.event('turn/end', { status: 'waiting_user' });
  f.pepsi.turns[0].resolve({ status: 'waiting_user' });
  await f.bridge.pumping;
  assert.equal(f.bridge.paused, true);
  await f.bridge.receive(message('写一封感谢信', 2));
  await until(() => f.pepsi.turns.length === 2);
  assert.equal(f.pepsi.turns[1].params.input, '写一封感谢信');
  f.pepsi.finish('好的');
  await f.bridge.pumping;
  await f.bridge.flush();
  assert.equal(f.texts().filter((text) => text === '写什么？').length, 1);
});

test('gateway disconnect buffers replies, ambiguous sends are not auto-retried', async (t) => {
  const f = await fixture(t);
  await f.bridge.receive(message('hello'));
  f.onebot.emit('disconnected');
  f.pepsi.finish('offline reply');
  await f.bridge.pumping;
  assert.equal(f.store.data.outbox.at(-1).text, 'offline reply');
  assert.equal(f.onebot.sent.length, 0);
  f.onebot.failSend = true;
  await f.bridge.connectGateway();
  assert.equal(f.store.data.outbox[0].status, 'uncertain');
  await f.bridge.connectGateway();
  assert.equal(f.onebot.sent.length, 0);
  await f.bridge.receive(message(`/resend ${f.store.data.outbox[0].id}`, 2));
  await f.bridge.flush();
  assert.equal(f.texts().filter((text) => text === 'offline reply').length, 1);
});

test('recovering interrupted work does not replay it, and pending input requires continue', async (t) => {
  const f = await fixture(t, { sessionId: 'aaaaaaaaaaaa', inFlight: { id: 'old', text: 'do not rerun' },
    inbox: [{ id: 'pending', text: 'safe queued task' }] });
  assert.equal(f.pepsi.turns.length, 0);
  assert.equal(f.store.data.lastInterrupted.text, 'do not rerun');
  assert.equal(f.pepsi.calls[0].method, 'session/resume');
  await f.bridge.receive(message('/continue', 1));
  await until(() => f.pepsi.turns.length === 1);
  assert.equal(f.pepsi.turns[0].params.input, 'safe queued task');
  f.pepsi.finish();
  await f.bridge.pumping;
});

test('new starts an independent Pepsi conversation, not a deletion of old history', async (t) => {
  const f = await fixture(t);
  const before = f.store.data.sessionId;
  await f.bridge.receive(message('/new'));
  assert.notEqual(f.store.data.sessionId, before);
  assert.equal(f.pepsi.calls.filter((call) => call.method === 'session/create').length, 2);
  assert.equal(f.pepsi.calls.some((call) => call.method === 'session/delete'), false);
});

test('restoring an old chat snapshot failure resumes only never-started queued messages', async (t) => {
  const f = await fixture(t, { sessionId: 'aaaaaaaaaaaa', inbox: [{ id: 'queued', text: '你好' }] }, {
    task: { kind: 'answer', status: 'blocked' },
    verification: { reason_code: 'verification_snapshot_unavailable' },
  });
  assert.equal(f.bridge.paused, false);
  assert.equal(f.pepsi.turns.length, 1);
  assert.equal(f.pepsi.turns[0].params.input, '你好');
  assert.equal(f.store.data.sessionId, 'aaaaaaaaaaaa');
  f.pepsi.finish();
  await f.bridge.pumping;
});

test('restoring an interrupted or blocked coding task still requires explicit continuation', async (t) => {
  for (const kind of ['change', 'operation']) {
    const f = await fixture(t, { sessionId: 'aaaaaaaaaaaa', inbox: [{ id: 'queued', text: 'pending' }] }, {
      task: { kind, status: 'blocked' }, verification: { reason_code: 'verification_snapshot_unavailable' },
    });
    assert.equal(f.bridge.paused, true);
    assert.equal(f.pepsi.turns.length, 0);
  }
  const interrupted = await fixture(t, {
    sessionId: 'aaaaaaaaaaaa', inFlight: { id: 'interrupted', text: 'do not replay' },
    inbox: [{ id: 'queued', text: 'pending' }],
  }, { task: { kind: 'answer', status: 'blocked' }, verification: { reason_code: 'verification_snapshot_unavailable' } });
  assert.equal(interrupted.bridge.paused, true);
  assert.equal(interrupted.pepsi.turns.length, 0);
});

test('new and restored sessions allow initialization time without extending ordinary requests', async (t) => {
  const fresh = await fixture(t);
  assert.equal(fresh.pepsi.calls.find((call) => call.method === 'session/create').options.timeoutMs, 120000);
  assert.equal(fresh.pepsi.calls.find((call) => call.method === 'permissions/set').options.timeoutMs, undefined);
  const restored = await fixture(t, { sessionId: 'aaaaaaaaaaaa' });
  assert.equal(restored.pepsi.calls.find((call) => call.method === 'session/resume').options.timeoutMs, 120000);
  assert.equal(restored.onebot.calls.find((call) => call.method === 'get_login_info').options.timeoutMs, undefined);
});

test('tokens are redacted, CQ output remains a text segment, and unsupported media never reaches the model', async (t) => {
  const f = await fixture(t);
  f.bridge.say('pepsi-secret-token qq-secret-token [CQ:at,qq=all]');
  await f.bridge.flush();
  assert.equal(f.texts()[0], '[已隐藏令牌] [已隐藏令牌] [CQ:at,qq=all]');
  assert.equal(f.onebot.sent[0].message[0].type, 'text');
  await f.bridge.receive(message('', 2, { message: [{ type: 'record', data: { file: 'x' } }] }));
  assert.equal(f.pepsi.turns.length, 0);
});

test('wrong gateway account cannot deliver or receive any messages', async (t) => {
  const f = await fixture(t);
  f.config.botQQ = '111111111';
  const count = f.onebot.sent.length;
  await assert.rejects(f.bridge.connectGateway(), /账号/);
  assert.equal(f.onebot.sent.length, count);
});

test('backend disconnect stops admission and never reissues an uncertain turn', async (t) => {
  const f = await fixture(t);
  await f.bridge.receive(message('work'));
  f.pepsi.connected = false;
  f.pepsi.emit('disconnected');
  f.pepsi.turns[0].reject(new Error('disconnected'));
  await f.bridge.pumping;
  await f.bridge.receive(message('another message', 2));
  assert.equal(f.fatal.length, 1);
  assert.equal(f.pepsi.turns.length, 1);
});
