import test from 'node:test';
import assert from 'node:assert/strict';
import { WebSocketServer } from 'ws';
import { RpcClient } from '../src/rpc.js';

async function server(t, onMessage) {
  const listener = new WebSocketServer({ host: '127.0.0.1', port: 0 });
  await new Promise((resolve) => listener.once('listening', resolve));
  listener.on('connection', (socket, request) => {
    socket.on('message', (raw) => onMessage(JSON.parse(raw.toString()), socket, request));
  });
  t.after(async () => {
    for (const client of listener.clients) client.terminate();
    await new Promise((resolve) => listener.close(resolve));
  });
  return `ws://127.0.0.1:${listener.address().port}`;
}

test('pepsi authenticates first and correlates concurrent RPC replies with event delivery', async (t) => {
  const methods = [];
  const url = await server(t, (packet, socket) => {
    methods.push(packet.method);
    if (packet.method === 'connection/auth') {
      assert.equal(packet.params.token, 'secret');
      socket.send(JSON.stringify({ kind: 'response', id: packet.id, result: { authenticated: true } }));
    } else {
      socket.send('invalid json');
      socket.send('null');
      socket.send(JSON.stringify({ kind: 'event', event: 'message/end', data: { content: 'hello' } }));
      setTimeout(() => socket.send(JSON.stringify({ kind: 'response', id: packet.id, result: packet.method })), packet.method === 'slow' ? 20 : 0);
    }
  });
  const client = new RpcClient({ url, token: 'secret', protocol: 'pepsi' });
  t.after(() => client.close());
  const events = [];
  client.on('notification', (packet) => events.push(packet));
  await client.connect();
  const results = await Promise.all([client.request('slow'), client.request('fast')]);
  assert.deepEqual(results, ['slow', 'fast']);
  assert.equal(methods[0], 'connection/auth');
  assert.equal(events.length, 2);
});

test('onebot uses bearer header and action/echo envelopes, sanitizes errors, and times out uncertain actions', async (t) => {
  const url = await server(t, (packet, socket, request) => {
    assert.equal(request.headers.authorization, 'Bearer onebot-secret');
    assert.equal(packet.kind, undefined);
    if (packet.action === 'get_login_info') socket.send(JSON.stringify({ echo: packet.echo, status: 'ok', retcode: 0, data: { user_id: 12345 } }));
    if (packet.action === 'fail') socket.send(JSON.stringify({ echo: packet.echo, status: 'failed', retcode: 1401, wording: 'onebot-secret' }));
  });
  const client = new RpcClient({ url, token: 'onebot-secret', protocol: 'onebot', timeoutMs: 30 });
  t.after(() => client.close());
  await client.connect();
  assert.equal((await client.request('get_login_info')).user_id, 12345);
  await assert.rejects(client.request('fail'), (error) => !error.message.includes('onebot-secret'));
  await assert.rejects(client.request('send_private_msg'), /结果未知/);
  assert.equal(client.pending.size, 0);
});

test('disconnect rejects pending requests instead of replaying commands on a fresh connection', async (t) => {
  const url = await server(t, (_packet, socket) => socket.close());
  const client = new RpcClient({ url, token: 'secret', protocol: 'onebot' });
  t.after(() => client.close());
  await client.connect();
  await assert.rejects(client.request('send_private_msg'), /断开/);
  assert.equal(client.pending.size, 0);
});
