import fs from 'node:fs';
import path from 'node:path';
import { randomUUID } from 'node:crypto';
import { atomicWriteJson } from '../vendor/qq-bridge/atomic-write.js';

export class StateStore {
  constructor(config) {
    this.dir = config.stateDir;
    fs.mkdirSync(this.dir, { recursive: true, mode: 0o700 });
    this.file = path.join(this.dir, 'private.json');
    this.lock = path.join(this.dir, 'bridge.lock');
    this.lockId = randomUUID();
    this.acquire();
    try {
      this.data = fs.existsSync(this.file)
        ? JSON.parse(fs.readFileSync(this.file, 'utf8'))
        : { version: 1, ownerQQ: config.ownerQQ, workspace: config.workspace, botQQ: null,
          sessionId: null, inbox: [], outbox: [], recentIds: [], inFlight: null };
      const d = this.data;
      if (d.version !== 1 || d.ownerQQ !== config.ownerQQ || d.workspace !== config.workspace
          || ![d.inbox, d.outbox, d.recentIds].every(Array.isArray)) {
        throw new Error('私聊状态与配置不匹配或已损坏。请备份 state 后使用新的状态目录');
      }
      if (!d.inbox.every((m) => m && typeof m.text === 'string' && typeof m.id === 'string')
          || !d.outbox.every((m) => m && typeof m.text === 'string' && typeof m.id === 'string'
            && ['pending', 'sending', 'uncertain'].includes(m.status))
          || !d.recentIds.every((id) => typeof id === 'string')
          || (d.sessionId !== null && !/^[a-f0-9]{12}$/.test(d.sessionId))) {
        throw new Error('私聊状态损坏，已停止以避免重复执行');
      }
      for (const item of d.outbox) if (item.status === 'sending') item.status = 'uncertain';
      this.save();
    } catch (error) {
      this.close();
      throw error;
    }
  }

  acquire() {
    for (let attempt = 0; attempt < 2; attempt++) {
      try {
        fs.writeFileSync(this.lock, JSON.stringify({ pid: process.pid, id: this.lockId }), { flag: 'wx', mode: 0o600 });
        return;
      } catch (error) {
        if (error.code !== 'EEXIST') throw error;
        let previous;
        try { previous = JSON.parse(fs.readFileSync(this.lock, 'utf8')); } catch {
          throw new Error('锁文件损坏；请确认没有运行中的桥接，再移走 state/bridge.lock');
        }
        if (!Number.isInteger(previous.pid) || previous.pid <= 0) throw new Error('无效的桥接锁文件');
        try { process.kill(previous.pid, 0); } catch (probe) {
          if (probe.code === 'ESRCH') { fs.unlinkSync(this.lock); continue; }
        }
        throw new Error('该 state 已有运行中的桥接，不能重复启动');
      }
    }
    throw new Error('无法获取桥接锁');
  }

  save() {
    atomicWriteJson(this.file, this.data);
  }

  close() {
    if (fs.existsSync(this.lock) && JSON.parse(fs.readFileSync(this.lock, 'utf8')).id === this.lockId) {
      fs.unlinkSync(this.lock);
    }
  }
}
