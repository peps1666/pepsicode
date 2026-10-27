import { spawn } from 'node:child_process';
import { randomBytes } from 'node:crypto';
import { fileURLToPath } from 'node:url';

export async function startBackend(config, { signal } = {}) {
  signal?.throwIfAborted();
  if (config.backend.mode === 'external') return { ...config.backend, stop: async () => {} };
  const token = randomBytes(32).toString('base64url');
  const child = spawn(config.backend.python, ['-m', 'pepsicode.server', '--host', '127.0.0.1', '--port', '0'], {
    cwd: fileURLToPath(new URL('../../../', import.meta.url)),
    env: { ...process.env, PEPSI_CODE_SERVER_TOKEN: token, PYTHONUNBUFFERED: '1', PYTHONIOENCODING: 'utf-8' },
    stdio: ['ignore', 'pipe', 'pipe'], windowsHide: true,
  });
  // Never print backend logs to QQ or relay environment/config credentials.
  child.stderr.resume();
  const stop = async () => {
    if (!child.pid || child.exitCode !== null || child.signalCode !== null) return;
    await new Promise((resolve) => {
      child.once('exit', resolve);
      child.kill();
    });
  };
  try {
    const url = await new Promise((resolve, reject) => {
      let output = '';
      const timer = setTimeout(() => reject(new Error('Python 服务启动超时，请检查 Python 环境和 pepsicode 安装')), 30000);
      const abort = () => finish(reject, new Error('Python 服务启动已取消'));
      const finish = (fn, value) => { clearTimeout(timer); signal?.removeEventListener('abort', abort); fn(value); };
      signal?.addEventListener('abort', abort, { once: true });
      if (signal?.aborted) return abort();
      child.once('error', () => finish(reject, new Error('无法启动 Python，请检查 backend.python')));
      child.once('exit', () => finish(reject, new Error('Python 服务已退出；请运行 python -m pepsicode.server 检查安装')));
      child.stdout.on('data', (chunk) => {
        output = (output + chunk.toString()).slice(-8192);
        const match = output.match(/pepsicode server listening on (ws:\/\/127\.0\.0\.1:\d+)/);
        if (match) finish(resolve, match[1]);
      });
    });
    return { url, token, stop, child };
  } catch (error) {
    await stop();
    throw error;
  }
}
