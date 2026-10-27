import fs from 'node:fs';
import path from 'node:path';

export function qqId(value) {
  if (typeof value === 'number' && !Number.isSafeInteger(value)) return null;
  const id = String(value ?? '');
  return /^[1-9]\d{4,14}$/.test(id) ? id : null;
}

function localSocket(value, label) {
  let url;
  try { url = new URL(value); } catch { throw new Error(`${label} 必须是 WebSocket 地址`); }
  if (!['ws:', 'wss:'].includes(url.protocol)
      || !['127.0.0.1', 'localhost', '[::1]'].includes(url.hostname)
      || url.username || url.password || url.search || url.hash) {
    throw new Error(`${label} 只允许本机 ws/wss 地址；令牌请放环境变量，不要放 URL`);
  }
  return url.href;
}

function integer(value, fallback, min, max, name) {
  const result = value ?? fallback;
  if (!Number.isInteger(result) || result < min || result > max) {
    throw new Error(`${name} 必须是 ${min} 到 ${max} 的整数`);
  }
  return result;
}

function envToken(name, env) {
  if (typeof name !== 'string' || !/^[A-Za-z_][A-Za-z0-9_]*$/.test(name)) {
    throw new Error('tokenEnv 必须是环境变量名称');
  }
  const value = env[name];
  if (typeof value !== 'string' || !value.trim() || /[\r\n]/.test(value)) {
    throw new Error(`请设置非空令牌环境变量 ${name}`);
  }
  return value;
}

export function loadConfig(filename, env = process.env) {
  const file = path.resolve(filename);
  const raw = JSON.parse(fs.readFileSync(file, 'utf8').replace(/^\uFEFF/, ''));
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) throw new Error('配置必须是 JSON 对象');
  const ownerQQ = qqId(raw.ownerQQ);
  const botQQ = raw.botQQ ? qqId(raw.botQQ) : null;
  if (!ownerQQ || (raw.botQQ && !botQQ) || ownerQQ === botQQ) {
    throw new Error('请填写有效且不同的 ownerQQ（你的 QQ）和可选 botQQ（Pepsi 的 QQ）');
  }
  if (typeof raw.workspace !== 'string' || !raw.workspace.trim()) throw new Error('请配置 workspace');
  const workspace = fs.realpathSync(path.resolve(path.dirname(file), raw.workspace));
  if (!fs.statSync(workspace).isDirectory()) throw new Error('workspace 必须是已有目录');
  const accessMode = raw.accessMode ?? 'auto';
  if (!['auto', 'read_only'].includes(accessMode)) throw new Error('accessMode 只允许 auto 或 read_only');
  const mode = raw.backend?.mode ?? 'managed';
  if (!['managed', 'external'].includes(mode)) throw new Error('backend.mode 只允许 managed 或 external');
  const python = raw.backend?.python ?? env.PEPSI_PYTHON ?? 'python';
  if (typeof python !== 'string' || !python.trim()) throw new Error('backend.python 必须是 Python 可执行文件路径');
  const limits = raw.limits ?? {};
  return {
    ownerQQ, botQQ, workspace, accessMode,
    stateDir: path.join(path.dirname(file), 'state'),
    onebot: {
      url: localSocket(raw.onebot?.wsUrl, 'onebot.wsUrl'),
      token: envToken(raw.onebot?.tokenEnv ?? 'PEPSI_QQ_ONEBOT_TOKEN', env),
    },
    backend: {
      mode, python,
      url: mode === 'external' ? localSocket(raw.backend?.url, 'backend.url') : null,
      token: mode === 'external' ? envToken(raw.backend?.tokenEnv ?? 'PEPSI_QQ_SERVER_TOKEN', env) : null,
    },
    limits: {
      queueSize: integer(limits.queueSize, 16, 1, 32, 'queueSize'),
      inputChars: integer(limits.inputChars, 12000, 100, 32000, 'inputChars'),
      messageChars: integer(limits.messageChars, 2000, 100, 4000, 'messageChars'),
      sendDelayMs: integer(limits.sendDelayMs, 500, 0, 10000, 'sendDelayMs'),
      outboxSize: integer(limits.outboxSize, 200, 50, 1000, 'outboxSize'),
    },
  };
}
