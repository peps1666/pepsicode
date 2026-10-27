# Pepsi：QQ 私聊好友

给 Pepsi 使用的 QQ 发文字，就能和它聊天，或请它处理配置好的本地项目。
Pepsi 会保留上下文、顺序回复消息；需要确认操作时，会把审批发到同一个私聊。

这是独立的消息通道，复用 pepsicode 的 Python Agent、模型配置、工具和权限系统。
QQ 收发使用 OneBot v11；Markdown 转文字、长消息拆分和 Windows 原子写入工具直接复用
[Derpyu520/qq-bridge](https://github.com/Derpyu520/qq-bridge) 的 MIT 代码。
来源版本和保留文件见 [UPSTREAM.md](vendor/qq-bridge/UPSTREAM.md)。

## 准备

- Node.js 22+、Python 3.11+，并已在仓库根目录执行 `python -m pip install -e .`。
- 已给 pepsicode 配置好模型，能在终端或桌面正常对话。
  如果设置了 `PEPSI_CODE_MODEL_MODE=mock`，启动的也是离线 mock，不能进行真实 AI 聊天。
- 一个已登录 QQ 的 OneBot v11 网关，例如参考项目使用的 SnowLuma。
  网关由你单独安装、启动和扫码登录，本项目不打包或自动安装网关。
- 建议让 Pepsi 使用一个单独的 QQ 账号，并让你的 QQ 与它互加好友。
  `ownerQQ` 填你自己的 QQ，`botQQ` 填 Pepsi 使用的 QQ。

在网关中启用**正向 WebSocket 服务**，设置非空 access token，允许调用 API 并上报消息。
消息格式优先选择消息段数组（array）。地址应类似 `ws://127.0.0.1:3001`；
填写网关实际提供的 WebSocket 地址，不要填 HTTP API 端口或反向 WebSocket 的回调地址。
网关和 bridge 默认在同一台电脑运行。

非官方 QQ 网关可能导致账号受限，实际能力和限制取决于网关；参考项目的
[第三方组件说明](https://github.com/Derpyu520/qq-bridge/blob/main/LICENSE) 也记录了这一点。

## 启动（Windows PowerShell）

在仓库根目录执行：

```powershell
cd integrations/qq
npm ci
Copy-Item config.example.json config.json
notepad config.json
```

编辑配置：

```json
{
  "ownerQQ": "你的QQ号",
  "botQQ": "Pepsi使用的QQ号",
  "workspace": "../..",
  "accessMode": "auto",
  "onebot": {
    "wsUrl": "ws://127.0.0.1:3001",
    "tokenEnv": "PEPSI_QQ_ONEBOT_TOKEN"
  },
  "backend": { "mode": "managed", "python": "python" }
}
```

- QQ 号建议使用 JSON 字符串；必须替换示例中的占位符。
- `workspace` 相对于 **config.json 所在目录**解析，默认 `../..` 指向 pepsicode 仓库。
  可改为另一个已有项目的绝对路径，如 `E:/Code/my-project`。
- `accessMode: auto` 沿用项目现有权限规则，项目内编辑通常直接执行，需要审批的操作会发到 QQ。
  只希望聊天、查看项目时可改为 `read_only`；不提供 `full_access` 快捷配置。
- `backend.python` 可填已安装 pepsicode 依赖的 Python 完整路径。
- `botQQ` 可留空，首次连通会绑定网关当前登录的 QQ；以后切换账号会拒绝连接，避免串号。

设置网关中配置的 access token，然后检查并启动：

```powershell
$env:PEPSI_QQ_ONEBOT_TOKEN = "替换成网关的access token"
npm run check
npm start
```

看到 `QQ 已连接，等待你的私聊` 后，用 `ownerQQ` 对应的 QQ 给 Pepsi 发「你好」。
首次连接会给你发送一条招呼消息。QQ 的头像、昵称由 QQ 账号本身设置，可在 QQ 中改为 Pepsi。
首次创建或恢复会话还会初始化模型目录、Skills 和 MCP，可能需要等待几十秒；
该阶段单独保留最多两分钟，普通状态查询和消息发送仍使用较短的超时。

`npm start` 默认自动拉起专用 Python 服务端，使用随机本机端口和本次运行的随机鉴权令牌。
不需要另开终端启动服务端；关闭桌面客户端也不影响这个独立进程。
保持启动终端、电脑和 QQ 网关运行；退出用 Ctrl+C。

macOS/Linux 的安装和启动命令相同，把设置环境变量的命令换成：

```bash
export PEPSI_QQ_ONEBOT_TOKEN='替换成网关的access token'
npm start
```

## 如何聊天

普通文字直接发送即可。忙时会顺序排队，一条消息对应一轮对话，不会打断当前执行。
Pepsi 提问时直接回复文字；不用先输入命令。
没有执行命令或修改文件的普通聊天不触发项目验收，也不会因之前的待验证修改暂停聊天。
简单日期、星期和时间问题使用每轮注入的本机时间；实际项目操作仍沿用工具审批与验证。

| 命令 | 含义 |
| --- | --- |
| `/help` | 查看用法 |
| `/status` | 查看忙闲、排队数、会话编号和权限 |
| `/new` | 开一段新对话；旧对话保留在 pepsicode 历史中 |
| `/stop` | 请求停止当前任务，并清空尚未开始的消息；不会撤销已完成操作 |
| `/continue` | 继续暂停的队列 |
| `/approve 完整编号` | 同意当前这一次审批 |
| `/deny 完整编号` | 拒绝当前审批 |
| `/outbox` | 查看待发送数量和发送结果未知的消息编号 |
| `/resend 编号` | 明确要求重发一条结果未知的消息，可能产生重复 |
| `/discard 编号` | 丢弃一条结果未知的消息 |

仅接收配置的 `ownerQQ` 发来的好友私聊。群消息、临时群会话、陌生人消息、自己发送的消息均不进入 Agent。
当前支持文字和文字引用；引用仅保留本条文字，不抓取被引用原文。
图片、表情、文件和语音会提示暂不支持；带这些内容的混合消息也不会被当作已看懂来回答。
未列出的斜杠命令不会透传给 Agent；自然语言仍可请求工具执行或项目操作。

## 断线与恢复

- QQ 网关掉线：桥接自动重连；当前 Agent 可以完成回复，结果先写入本地待发送队列。
  网关恢复并通过账号校验后，发送尚未尝试投递的消息。
- 发送超时或连接在发送途中断开：结果可能已经送达，标记为「结果未知」，不自动重发。
  使用 `/outbox` 核对，再决定 `/resend` 或 `/discard`。
- 进程重启：恢复映射的 pepsicode 会话和聊天上下文；正在运行的输入不自动重跑。
  上次未开始的排队消息会暂停，发 `/continue` 后继续。
- Python 服务连接中断：桥接停止，避免在不清楚任务结果时重新执行。修复后重启，再核对任务。
- 队列有容量限制。输入队列满时会明确告知本条未接收；待发送队列满时暂停后续任务，
  溢出的回复保留在 pepsicode 会话历史，并在恢复后提醒查看。
- 审批约 5 分钟有效，只能由配置的 QQ 用当前完整编号回复；过期或重复回复不会生效。

桌面端可以在任务停止后恢复同一个项目下的历史会话查看；
当前不支持桌面与 QQ 同时控制同一个运行中的会话。不要在两个入口同时恢复并执行同一会话。

## 目录与本地数据

```text
integrations/qq/
  config.example.json          可提交的脱敏示例
  config.json                  你的配置，Git 忽略
  src/
    main.js                    启动、退出和网关重连
    config.js                  配置验证
    backend.js                 专用 Python 服务生命周期
    rpc.js                     pepsicode / OneBot WebSocket 协议
    bridge.js                  私聊、队列、审批和回复
    store.js                   状态持久化与单实例锁
  vendor/qq-bridge/             复用的上游代码、许可证和版本说明
  tests/                       离线测试、真实 Python 服务端端到端测试
  state/                       会话映射、收发队列、消息去重和锁，Git 忽略
```

完整聊天历史由 pepsicode 原有会话系统保存在 `~/.pepsi-code/sessions/`。
`state/private.json` 含未处理消息、待发送回复和会话映射；不要提交或分享。
令牌从环境变量读取，不写入 bridge 状态或发往 QQ；聊天内容仍会交给你配置的模型提供方处理。

可以用 `npm start -- --config 路径/config.json` 指定另一份配置。
状态固定放在该配置旁的 `state/`；不同账号/项目应使用不同配置目录。
换账号或换项目前先退出桥接，并备份旧目录，不要直接把旧映射绑定给新账号。

需要接入已经运行的独立服务端时，将 backend 改为：

```json
{
  "mode": "external",
  "url": "ws://127.0.0.1:8766",
  "tokenEnv": "PEPSI_QQ_SERVER_TOKEN"
}
```

另行给服务端设置 `PEPSI_CODE_SERVER_TOKEN`，桥接设置相同值的 `PEPSI_QQ_SERVER_TOKEN`。
只接受本机连接地址；不连接 Electron 自动生成、令牌未对外提供的服务端。

## 验证

```powershell
npm test
npm run test:e2e
```

测试不登录真实 QQ，不调用付费模型。端到端测试模拟 OneBot 网关，实际运行 Python 服务端、
Agent Loop、工具审批、对话保存与恢复。真实 QQ 网关兼容性仍需登录后验证。
