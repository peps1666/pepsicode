import { randomUUID } from 'node:crypto';
import { setTimeout as delay } from 'node:timers/promises';
import { qqId } from './config.js';
import { mdToPlain, splitForQQ, truncateText } from '../vendor/qq-bridge/md-to-plain.js';

const HELP = `我是 Pepsi，可以陪你聊聊，也能帮你处理配置好的本地项目。
直接发文字就好，忙的时候我会按顺序回复。
/help 帮助 · /status 状态 · /new 新对话
/stop 停止当前任务并清空排队 · /continue 继续暂停的队列
/approve 编号 同意一次 · /deny 编号 拒绝一次
/outbox 查看待发送消息 · /resend 编号 重发结果未知的消息 · /discard 编号 丢弃该消息
目前支持私聊文字，图片和语音还看不了。`;

// Initializing a real session also discovers models, skills and MCP servers.
// Keep this separate from the short timeout for ordinary status/send RPCs.
const SESSION_INIT_TIMEOUT_MS = 120000;

export function privateText(packet, ownerQQ, selfQQ) {
  if (packet.post_type !== 'message' || packet.message_type !== 'private'
      || packet.sub_type !== 'friend' || packet.group_id != null
      || qqId(packet.user_id) !== ownerQQ || qqId(packet.self_id) !== selfQQ
      || qqId(packet.user_id) === selfQQ) return null;
  if (Array.isArray(packet.message)) {
    if (!packet.message.every((part) => part && typeof part === 'object' && typeof part.type === 'string')) return '';
    // Do not silently answer an image caption as if the image had been seen.
    if (packet.message.some((part) => !['text', 'reply'].includes(part.type))) return '';
    return packet.message.filter((part) => part.type === 'text').map((part) => String(part.data?.text ?? '')).join('').trim();
  }
  if (typeof packet.message !== 'string') return '';
  if (/\[CQ:(?!reply,)/.test(packet.message)) return '';
  return packet.message.replace(/\[CQ:reply,[^\]]*\]/g, '')
    .replace(/&#91;/g, '[').replace(/&#93;/g, ']').replace(/&#44;/g, ',').replace(/&amp;/g, '&').trim();
}

export class PrivateBridge {
  constructor({ config, store, pepsi, onebot, log = console.log, onFatal = () => {} }) {
    Object.assign(this, { config, store, pepsi, onebot, log, onFatal });
    this.selfQQ = null;
    this.busy = false;
    this.paused = false;
    this.stopping = false;
    this.backendReady = false;
    this.gatewayReady = false;
    this.approvals = new Map();
    this.incoming = Promise.resolve();
    this.flushing = null;
    this.pumping = null;
    this.turnOutput = false;
    this.awaitingReply = false;
    this.pepsi.on('notification', (packet) => {
      try { this.backendEvent(packet); } catch { this.fail('保存回复失败，桥接已停止。请检查 state 目录和磁盘空间。'); }
    });
    this.pepsi.on('disconnected', () => {
      this.backendReady = false;
      if (!this.stopping) this.fail('Pepsi 服务连接中断，任务结果可能未知。请重启桥接后查看状态，不会自动重跑。');
    });
    this.onebot.on('notification', (packet) => {
      this.incoming = this.incoming.then(() => this.receive(packet)).catch(() => {
        this.fail('私聊消息处理失败，已停止以避免重复执行。请检查配置或 state 目录。');
      });
    });
    this.onebot.on('disconnected', () => { this.gatewayReady = false; });
  }

  get state() { return this.store.data; }

  redact(text) {
    let result = String(text);
    for (const token of [this.config.onebot.token, this.pepsi.token]) {
      if (token) result = result.replaceAll(token, '[已隐藏令牌]');
    }
    return result;
  }

  say(text) {
    if (!text) return;
    const plain = truncateText(mdToPlain(this.redact(text)), 40000);
    const chunks = splitForQQ(plain, this.config.limits.messageChars);
    if (String(text).length > 40000) chunks.push('回复过长，以上仅显示前 40000 字符。完整内容请查看 pepsicode 会话历史。');
    if (this.state.outbox.length + chunks.length > this.config.limits.outboxSize) {
      // Keep the authoritative response in pepsicode history, signal loss explicitly.
      this.state.outputOverflow = true;
      this.paused = true;
      this.store.save();
      this.log('待发送队列已满，已暂停新任务。完整回复保留在 pepsicode 会话历史。');
      return;
    }
    this.state.outbox.push(...chunks.map((content) => ({ id: randomUUID().slice(0, 8), text: content, status: 'pending' })));
    this.store.save();
    void this.flush();
  }

  flush() {
    if (this.flushing) return this.flushing;
    this.flushing = this.flushLoop().catch((error) => this.fail(`保存发送状态失败（${error.code ?? error.name}），桥接已停止。`))
      .finally(() => {
        this.flushing = null;
        if (this.gatewayReady && !this.stopping && this.state.outbox.some((item) => item.status === 'pending')) {
          queueMicrotask(() => { void this.flush(); });
        }
      });
    return this.flushing;
  }

  async flushLoop() {
    while (this.gatewayReady && !this.stopping) {
      const item = this.state.outbox.find((entry) => entry.status === 'pending');
      if (!item) break;
      item.status = 'sending';
      this.store.save();
      try {
        await this.onebot.request('send_private_msg', {
          user_id: Number(this.config.ownerQQ),
          // Message arrays avoid interpreting model output as CQ codes/actions.
          message: [{ type: 'text', data: { text: item.text } }],
        });
        this.state.outbox = this.state.outbox.filter((entry) => entry.id !== item.id);
      } catch {
        item.status = 'uncertain';
        this.log(`消息 ${item.id} 发送结果未知；不会自动重发，可在 QQ 使用 /outbox 查看。`);
        // Pause on timeout as well as disconnect: don't flood an unhealthy gateway.
        this.gatewayReady = false;
      }
      this.store.save();
      if (this.gatewayReady) await delay(this.config.limits.sendDelayMs);
    }
    if (this.state.outputOverflow && this.state.outbox.length < this.config.limits.outboxSize - 1 && this.gatewayReady) {
      this.state.outputOverflow = false;
      this.say('离线期间待发送队列曾达到上限，部分回复请到 pepsicode 历史中查看。任务队列已暂停，/continue 可继续。');
    }
  }

  async start() {
    await this.pepsi.connect();
    let resumeChatQueue = false;
    if (this.state.sessionId) {
      const restored = await this.pepsi.request('session/resume', {
        session_id: this.state.sessionId, workspace: this.config.workspace,
      }, { timeoutMs: SESSION_INIT_TIMEOUT_MS });
      if (restored.profile !== 'qq_private') throw new Error('保存的会话不是 Pepsi 私聊，请备份并移走 state 后重新启动');
      this.awaitingReply = ['waiting_user', 'blocked'].includes(restored.task?.status);
      this.mock = restored.isMock === true;
      // Recover the old false pause without replaying any interrupted input or
      // granting completion to a coding task. New work keeps normal verification.
      resumeChatQueue = !this.state.inFlight && restored.task?.kind === 'answer'
        && restored.task?.status === 'blocked'
        && restored.verification?.reason_code === 'verification_snapshot_unavailable';
    } else {
      await this.newSession();
    }
    await this.pepsi.request('permissions/set', { mode: this.config.accessMode });
    this.backendReady = true;
    if (this.mock) this.say('当前运行的是离线演示模型，还不能正常 AI 聊天。请先在 pepsicode 配置模型，关闭 mock 模式后重启桥接。');
    const uncertain = this.state.outbox.filter((item) => item.status === 'uncertain').length;
    if (uncertain) this.say(`上次有 ${uncertain} 条消息发送结果未知，不会自动重发。发 /outbox 可以核对。`);
    if (resumeChatQueue) {
      this.awaitingReply = false;
    } else if (this.state.inFlight || this.state.inbox.length) {
      this.paused = true;
      if (this.state.inFlight) {
        this.state.lastInterrupted = this.state.inFlight;
        this.state.inFlight = null;
      }
      this.store.save();
      this.say(`上次连接中断或退出，正在运行的消息不会自动重跑。还有 ${this.state.inbox.length} 条未开始的消息，/continue 可继续；/stop 可清空。`);
    }
  }

  async newSession() {
    const created = await this.pepsi.request('session/create',
      { cwd: this.config.workspace, profile: 'qq_private' }, { timeoutMs: SESSION_INIT_TIMEOUT_MS });
    if (created.profile !== 'qq_private') throw new Error('服务端版本过旧，不支持 Pepsi 私聊，请更新 pepsicode');
    this.state.sessionId = created.session_id;
    this.mock = created.isMock === true;
    this.store.save();
  }

  async connectGateway() {
    await this.onebot.connect();
    const info = await this.onebot.request('get_login_info');
    const id = qqId(info?.user_id);
    if (!id || id === this.config.ownerQQ || (this.config.botQQ && id !== this.config.botQQ)
        || (this.state.botQQ && id !== this.state.botQQ)) {
      await this.onebot.close();
      throw new Error('QQ 网关登录账号与配置或历史绑定不一致，请确认 Pepsi 使用的 QQ 账号');
    }
    this.selfQQ = id;
    this.state.botQQ = id;
    this.gatewayReady = true;
    this.store.save();
    if (!this.state.introduced) {
      this.state.introduced = true;
      this.say('嗨，我是 Pepsi，已经连接好啦。直接发文字就能跟我聊天，需要时我也能帮你处理本地项目。发 /help 看用法。');
    }
    await this.flush();
    void this.pump();
  }

  async receive(packet) {
    if (!this.gatewayReady || this.stopping) return;
    const text = privateText(packet, this.config.ownerQQ, this.selfQQ);
    if (text === null) return;
    const messageId = packet.message_id;
    if (!['string', 'number'].includes(typeof messageId)
        || (typeof messageId === 'number' && !Number.isSafeInteger(messageId))) return;
    const id = `${this.selfQQ}:${String(messageId)}`;
    if (this.state.recentIds.includes(id)) return;
    // Persist admission and dedupe before any model request or command side effect.
    this.state.recentIds.push(id);
    this.state.recentIds = this.state.recentIds.slice(-2048);
    if (!text) {
      this.store.save();
      this.say('这条消息我暂时读不了，现在只支持文字私聊。可以把图片或语音里的内容打字告诉我。');
      return;
    }
    if (text.length > this.config.limits.inputChars) {
      this.store.save();
      this.say(`消息有点长，请分成每条不超过 ${this.config.limits.inputChars} 字符再发。`);
      return;
    }
    if (text.startsWith('/')) {
      this.store.save();
      await this.command(text);
      return;
    }
    if (!this.backendReady) {
      this.store.save();
      this.say('Pepsi 服务暂时不可用，请重启桥接后再试。');
      return;
    }
    if (this.state.inbox.length >= this.config.limits.queueSize
        || this.state.outbox.length > this.config.limits.outboxSize - 30) {
      this.store.save();
      this.say('我这里排队的消息有点多，这条还没有接下。稍等一会儿再发给我吧。');
      return;
    }
    if (this.awaitingReply) {
      // A fresh answer to Pepsi's question takes precedence over older queued chat.
      this.state.inbox.unshift({ id, text });
      this.awaitingReply = false;
      this.paused = false;
    } else {
      this.state.inbox.push({ id, text });
    }
    this.store.save();
    if (this.busy || this.paused) this.say(this.paused
      ? '队列已暂停，消息已保存。发 /continue 继续，或 /stop 清空。'
      : `收到，我忙完手头这条就回复你，前面还有 ${this.state.inbox.length - 1} 条。`);
    void this.pump();
  }

  async command(text) {
    const [command, ...args] = text.split(/\s+/);
    const arg = args.join(' ');
    if (command === '/help') return this.say(HELP);
    if (command === '/status') {
      return this.say(`Pepsi ${this.busy ? '正在回复' : '空闲'}${this.paused ? '，队列已暂停' : ''}。\n排队 ${this.state.inbox.length} 条，待发送 ${this.state.outbox.length} 条。\n上轮状态：${this.state.lastStatus ?? '尚未开始'}\n权限：${this.config.accessMode}；会话：${this.state.sessionId ?? '未创建'}`);
    }
    if (command === '/outbox') {
      const uncertain = this.state.outbox.filter((item) => item.status === 'uncertain');
      return this.say(`待发送 ${this.state.outbox.filter((item) => item.status === 'pending').length} 条，发送结果未知 ${uncertain.length} 条。\n${uncertain.map((item) => `${item.id}：${truncateText(item.text, 80)}`).join('\n')}\n结果未知的消息不会自动重发。/resend 编号 可重发（可能重复），/discard 编号 可丢弃。`);
    }
    if (['/resend', '/discard'].includes(command)) {
      const item = this.state.outbox.find((entry) => entry.id === arg && entry.status === 'uncertain');
      if (!item) return this.say('没有这个结果未知的消息编号。发 /outbox 查看。');
      if (command === '/resend') item.status = 'pending';
      else this.state.outbox = this.state.outbox.filter((entry) => entry !== item);
      this.store.save();
      this.say(command === '/resend' ? '已按你的要求安排重发，可能与上一次重复。' : '已丢弃这条待核对消息。');
      return;
    }
    if (command === '/stop') {
      this.paused = true;
      this.awaitingReply = false;
      this.state.inbox = [];
      this.approvals.clear();
      this.store.save();
      if (this.busy && this.backendReady) await this.pepsi.request('turn/cancel');
      this.say('已清空排队消息，并请求停止当前任务。已经完成的操作不会撤销。发 /continue 或等停止后发 /new 继续。');
      return;
    }
    if (command === '/continue') {
      if (!this.backendReady) return this.say('Pepsi 服务未连接，请重启桥接。');
      this.paused = false;
      this.say('好，继续聊。');
      void this.pump();
      return;
    }
    if (command === '/new') {
      if (this.busy) return this.say('我还在处理当前消息，先发 /stop，等任务停止后再发 /new。');
      this.state.inbox = [];
      this.state.lastInterrupted = null;
      this.store.save();
      this.approvals.clear();
      await this.newSession();
      await this.pepsi.request('permissions/set', { mode: this.config.accessMode });
      this.paused = false;
      this.awaitingReply = false;
      return this.say('好，我们开一段新对话。之前的对话仍保留在 pepsicode 历史里。');
    }
    if (['/approve', '/deny'].includes(command)) {
      const expires = this.approvals.get(arg);
      if (!expires || Date.now() >= expires || !this.busy) {
        this.approvals.delete(arg);
        return this.say('这个审批已过期或不存在。请使用当前消息里的完整编号。');
      }
      const result = await this.pepsi.request('tool/approve', {
        prompt_id: arg, decision: command === '/approve' ? 'allow_once' : 'deny_once',
      });
      this.approvals.delete(arg);
      return this.say(result.resolved ? (command === '/approve' ? '已同意这一次操作。' : '已拒绝这一次操作。') : '审批已结束，这次回复没有生效。');
    }
    this.say('暂不支持这条命令，发 /help 看用法。普通聊天直接发文字就好。');
  }

  backendEvent(packet) {
    if (packet.kind !== 'event' || !this.busy) return;
    const data = packet.data ?? {};
    if (packet.event === 'permission/request' && typeof data.prompt_id === 'string') {
      this.approvals.set(data.prompt_id, Date.now() + 295000);
      this.say(`需要你确认一次操作：\n${data.summary ?? data.kind}\n${truncateText((data.details ?? []).join('\n'), 2500)}\n同意：/approve ${data.prompt_id}\n拒绝：/deny ${data.prompt_id}\n约 5 分钟内有效，未回复将拒绝。`);
    }
    if (packet.event === 'message/end' && data.role !== 'progress' && data.content) {
      this.turnOutput = true;
      this.say(data.content);
    }
    // ask_user is also emitted as message/end by the server; forwarding its
    // tool/result as well would send the same question twice.
    if (packet.event === 'turn/end') {
      this.state.lastStatus = data.status;
      this.approvals.clear();
      if (['cancelled', 'error'].includes(data.status)) this.say(data.status === 'cancelled' ? '这次任务已停止。' : '这次处理遇到了问题，请查看本地服务日志后重试。');
      if (['waiting_user', 'awaiting_user', 'blocked', 'budget_exhausted'].includes(data.status)) this.paused = true;
      this.awaitingReply = ['waiting_user', 'awaiting_user', 'blocked'].includes(data.status);
      if (data.status === 'blocked') this.say('这次任务还没有通过完成验证，队列已暂停。可以发补充说明继续，或发 /new 开一段新对话。');
      if (data.status === 'budget_exhausted') this.say('这次任务达到用量或执行限制，队列已暂停。请检查本地设置后发 /continue 继续。');
    }
  }

  pump() {
    if (this.pumping) return this.pumping;
    this.pumping = this.pumpLoop().catch(() => this.fail('任务状态保存失败，桥接已停止以避免重复执行。'))
      .finally(() => {
        this.pumping = null;
        if (!this.stopping && !this.paused && this.backendReady && this.gatewayReady && this.state.inbox.length) {
          queueMicrotask(() => { void this.pump(); });
        }
      });
    return this.pumping;
  }

  async pumpLoop() {
    while (!this.stopping && !this.paused && this.backendReady && this.gatewayReady && this.state.inbox.length) {
      this.busy = true;
      this.turnOutput = false;
      this.state.inFlight = this.state.inbox.shift();
      this.store.save();
      try {
        const result = await this.pepsi.request('turn/run', { input: this.state.inFlight.text }, { timeoutMs: 0 });
        if (!this.turnOutput) this.say(result.last_message || '这次没有生成回复，可以换个说法再发给我。');
        this.state.inFlight = null;
        this.store.save();
      } catch {
        this.paused = true;
        this.state.lastInterrupted = this.state.inFlight;
        this.state.inFlight = null;
        this.store.save();
        this.say('这次处理未能确认完成，不会自动重跑。请先检查 /status，再决定是否重新发送。排队消息可用 /continue 继续。');
      } finally {
        this.busy = false;
        this.approvals.clear();
      }
    }
  }

  fail(message) {
    if (this.stopping) return;
    this.stopping = true;
    this.log(message);
    this.onFatal(message);
  }

  async close() {
    this.stopping = true;
    if (this.busy && this.pepsi.connected) {
      await this.pepsi.request('turn/cancel', {}, { timeoutMs: 2000 }).catch(() => {});
      await Promise.race([this.pumping, delay(4000)]);
    }
    await this.pepsi.close();
    await this.onebot.close();
    if (this.pumping) await this.pumping;
    if (this.flushing) await this.flushing;
    await this.incoming;
  }
}
