import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { setTimeout as delay } from 'node:timers/promises';
import { loadConfig } from './config.js';
import { startBackend } from './backend.js';
import { RpcClient } from './rpc.js';
import { StateStore } from './store.js';
import { PrivateBridge } from './bridge.js';

const root = fileURLToPath(new URL('../', import.meta.url));
const args = process.argv.slice(2);
let filename = path.join(root, 'config.json');
let check = false;
for (let i = 0; i < args.length; i++) {
  if (args[i] === '--config' && args[i + 1]) filename = path.resolve(args[++i]);
  else if (args[i] === '--check') check = true;
  else if (args[i] === '--help') {
    console.log('Pepsi QQ 私聊：npm start -- [--config 路径] [--check]\n先复制 config.example.json 为 config.json，并设置 QQ 网关令牌环境变量。');
    process.exit(0);
  } else {
    console.error('未知参数。使用 --help 查看用法。');
    process.exit(1);
  }
}

let store, backend, bridge;
let stopping = false;
const startupAbort = new AbortController();
let done;
const finished = new Promise((resolve) => { done = resolve; });
const log = (text) => console.log(`[Pepsi] ${text}`);
async function shutdown(code = 0) {
  if (stopping) return;
  stopping = true;
  startupAbort.abort();
  process.exitCode = code;
  try { await bridge?.close(); }
  finally {
    await backend?.stop();
    store?.close();
    done();
  }
}
process.once('SIGINT', () => { void shutdown(); });
process.once('SIGTERM', () => { void shutdown(); });
try {
  const config = loadConfig(filename);
  if (check) {
    log('配置检查通过。尚未登录 QQ 或发送消息；实际连通性由启动后的网关探测验证。');
  } else {
    store = new StateStore(config);
    backend = await startBackend(config, { signal: startupAbort.signal });
    bridge = new PrivateBridge({
      config, store, log,
      pepsi: new RpcClient({ ...backend, protocol: 'pepsi' }),
      onebot: new RpcClient({ ...config.onebot, protocol: 'onebot' }),
      onFatal: () => { void shutdown(1); },
    });
    await bridge.start();
    log('Pepsi 对话服务就绪，正在连接 QQ 网关。');
    const reconnect = async () => {
      let backoff = 1000;
      while (!stopping) {
        if (!bridge.gatewayReady) {
          try {
            // Recreate the transport after a timed-out action, as well as disconnect.
            await bridge.onebot.close();
            await bridge.connectGateway();
            log('QQ 已连接，等待你的私聊。');
            backoff = 1000;
          } catch {
            log('QQ 网关连接或账号校验失败；检查网关是否登录、令牌、botQQ 和历史账号绑定，将自动重试。');
            backoff = Math.min(backoff * 2, 30000);
          }
        }
        await Promise.race([delay(backoff), finished]);
      }
    };
    await reconnect();
  }
} catch (error) {
  // Config errors have useful field names; socket/child errors are sanitized upstream.
  if (!stopping) console.error(`[Pepsi] 启动失败：${error instanceof SyntaxError ? '配置或状态 JSON 格式错误' : error.message}`);
  await shutdown(1);
}
