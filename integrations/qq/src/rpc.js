import { EventEmitter } from 'node:events';
import { randomUUID } from 'node:crypto';
import WebSocket from 'ws';

// Both protocols share a socket lifecycle, but keep their wire envelopes intact.
export class RpcClient extends EventEmitter {
  constructor({ url, token, protocol, timeoutMs = 15000 }) {
    super();
    Object.assign(this, { url, token, protocol, timeoutMs });
    this.pending = new Map();
    this.socket = null;
  }

  get connected() { return this.socket?.readyState === WebSocket.OPEN; }

  async connect() {
    if (this.connected) return;
    const socket = new WebSocket(this.url, {
      headers: this.protocol === 'onebot' ? { Authorization: `Bearer ${this.token}` } : {},
      handshakeTimeout: 10000, maxPayload: 2 * 1024 * 1024, followRedirects: false,
    });
    this.socket = socket;
    socket.on('message', (raw, binary) => {
      if (binary || this.socket !== socket) return;
      let packet;
      try { packet = JSON.parse(raw.toString()); } catch { return; }
      if (!packet || typeof packet !== 'object' || Array.isArray(packet)) return;
      const id = this.protocol === 'pepsi' ? (packet.kind === 'response' ? packet.id : null) : packet.echo;
      const pending = this.pending.get(id);
      if (pending) {
        this.pending.delete(id);
        clearTimeout(pending.timer);
        if (this.protocol === 'pepsi' ? !!packet.error : packet.status !== 'ok' || packet.retcode !== 0) {
          // Do not relay raw upstream errors; they can include local tokens or URLs.
          pending.reject(new Error(`${this.protocol} ${pending.method} 请求失败`));
        } else {
          pending.resolve(this.protocol === 'pepsi' ? packet.result : packet.data);
        }
      } else {
        this.emit('notification', packet);
      }
    });
    socket.on('error', () => {}); // Report via connect rejection / close, without raw credentials.
    socket.on('close', () => {
      clearInterval(socket.heartbeat);
      if (this.socket !== socket) return;
      for (const item of this.pending.values()) {
        clearTimeout(item.timer);
        item.reject(new Error(`${this.protocol} 连接已断开`));
      }
      this.pending.clear();
      this.emit('disconnected');
    });
    try {
      await new Promise((resolve, reject) => {
        socket.once('open', resolve);
        socket.once('error', () => reject(new Error(`${this.protocol} 无法连接，请检查地址、令牌和服务状态`)));
        socket.once('close', () => reject(new Error(`${this.protocol} 连接关闭`)));
      });
      let alive = true;
      socket.on('pong', () => { alive = true; });
      socket.heartbeat = setInterval(() => {
        if (!alive) return socket.terminate();
        alive = false;
        socket.ping();
      }, 20000);
      socket.heartbeat.unref();
      if (this.protocol === 'pepsi') {
        const result = await this.request('connection/auth', { token: this.token });
        if (result?.authenticated !== true) throw new Error('pepsi 鉴权失败');
      }
    } catch (error) {
      socket.terminate();
      throw error;
    }
  }

  request(method, params = {}, { timeoutMs = this.timeoutMs } = {}) {
    if (!this.connected) return Promise.reject(new Error(`${this.protocol} 未连接`));
    const id = randomUUID();
    const packet = this.protocol === 'pepsi'
      ? { kind: 'request', id, method, params }
      : { action: method, params, echo: id };
    return new Promise((resolve, reject) => {
      const timer = timeoutMs > 0 ? setTimeout(() => {
        this.pending.delete(id);
        reject(new Error(`${this.protocol} ${method} 请求超时，结果未知`));
      }, timeoutMs) : null;
      this.pending.set(id, { resolve, reject, timer, method });
      this.socket.send(JSON.stringify(packet), (error) => {
        if (!error || !this.pending.has(id)) return;
        clearTimeout(timer);
        this.pending.delete(id);
        reject(new Error(`${this.protocol} ${method} 发送失败，结果未知`));
      });
    });
  }

  async close() {
    const socket = this.socket;
    if (!socket || socket.readyState === WebSocket.CLOSED) return;
    await new Promise((resolve) => {
      const timer = setTimeout(() => socket.terminate(), 1000);
      socket.once('close', () => { clearTimeout(timer); resolve(); });
      socket.close();
    });
  }
}
