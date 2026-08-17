# pepsicode v2

pepsicode 是一个面向本地开发工作流的 Python 编程代理，提供终端 CLI 和 Electron 桌面客户端两种使用方式。第二版在原有工具、MCP、Skills、子代理、Plan 模式和上下文压缩能力之上，新增了可配置、可审计且受权限系统约束的 Hooks v2，以及基于 WebSocket 的桌面客户端架构。

> English summary: pepsicode v2 is a Python coding agent with permission-aware tools, Plan mode, context compaction, MCP/Skills/sub-agents, a secure configurable hook engine, and an Electron desktop client connected via WebSocket.

## 第二版亮点

- Hooks v2：支持工具前后、代理、会话、输入输出、压缩和应用生命周期事件。
- 安全动作：`deny`、`notify`、`context`、`command`，其中命令 Hook 复用现有权限审批。
- 项目信任：项目 Hook 默认禁用，用户确认后按文件内容指纹信任；文件变化后自动失效。
- Plan 模式隔离：命令 Hook 在 Plan 模式中强制禁用，不能绕过只读边界。
- 同步优先执行：关键的工具前置 Hook 可确定性阻断；后台动作由受管线程池执行并在退出时收敛。
- 临时上下文注入：Hook 生成的上下文只进入下一次模型请求，不写入持久会话历史。
- 完整生命周期：主代理与子代理共用 Hook 引擎，同时保留各自的 scope。
- 统一版本：包、TUI 和 MCP 客户端标识均为 `2.0.0`。
- Electron 桌面客户端：基于 React + Vite 的白色主题界面，通过 WebSocket 连接后端，支持项目文件夹选择、会话管理、流式输出和权限审批。

## 环境要求

- Python 3.11+
- Windows、macOS 或 Linux
- 一个 Anthropic 兼容接口；也可以使用内置 mock 模型进行本地验证

## 安装与启动

```bash
git clone https://github.com/peps1666/pepsicode.git
cd pepsicode
python -m pip install -e ".[dev]"
python -m pepsicode.main --install
```

安装后可以直接运行：

```bash
pepsicode
```

也可以不安装命令入口：

```bash
python -m pepsicode.main
```

使用 mock 模型启动：

```powershell
$env:PEPSI_CODE_MODEL_MODE = "mock"
python -m pepsicode.main
```

```bash
PEPSI_CODE_MODEL_MODE=mock python -m pepsicode.main
```

## Electron 桌面客户端

pepsicode 提供一个独立的 Electron 桌面应用，采用白色主题界面，体验类似 Codex / Claude Desktop，可以在图形界面里选择项目文件夹并与代理对话。

### 架构

```text
┌─────────────────────┐     WebSocket      ┌──────────────────────┐
│  Electron Client     │ ◄──────────────► │  Python Server        │
│  (React + Vite)      │    ws://127.0.0.1 │  (pepsicode.server)   │
│                      │                   │                       │
│  - 白色主题 UI        │                   │  - Agent Loop         │
│  - 项目文件夹选择      │                   │  - Tools / Skills     │
│  - 会话列表           │                   │  - Permissions        │
│  - 流式消息展示        │                   │  - Hooks v2           │
│  - 权限审批弹窗        │                   │  - Context Manager    │
└─────────────────────┘                   └──────────────────────┘
```

Electron 主进程在启动时自动拉起 Python WebSocket 服务端，前端通过 `ws://127.0.0.1:<port>` 连接后端，使用 JSON-RPC 信封通信，支持流式事件（`message/delta`、`tool/call`、`tool/result` 等）和权限请求。

### 环境要求

- Node.js 18+
- Python 3.11+（同 CLI 要求）
- 已安装 pepsicode 包（`pip install -e .`）

### 构建与启动

```bash
cd pepsicode-client
npm install
npm run build      # 构建前端 (dist/) 和 Electron 主进程 (dist-electron/)
npm start          # 启动桌面应用
```

开发模式（热重载）：

```bash
# 终端 1：启动 Vite dev server
PEPSI_DEV_SERVER=1 npm run dev

# 终端 2：启动 Electron（连热重载）
npm start
```

### 选择项目文件夹

侧边栏顶部有一个文件夹按钮，点击后弹出系统文件夹选择框。选择后，代理会在新选的项目目录里工作（工具执行、文件读写、命令运行的 cwd 都切换到新目录）。

切换项目会重新创建会话，清空当前对话历史。

### 模型配置

桌面客户端共用同一套配置文件（`~/.pepsi-code/settings.json`），模型和 API Key 的读取优先级与 CLI 一致（见下方"模型配置"章节）。

界面顶部 header 左侧显示当前模型名称，右侧显示当前工作目录。

### 打包为独立 exe

```bash
cd pepsicode-client
npm run electron:build    # 输出到 release/ 目录
```

打包后的应用内嵌 Python 解释器，可直接分发给未安装 Python 环境的用户（需配合 PyInstaller 打包 `pepsicode` 包，具体见打包脚本）。

## 模型配置

用户配置文件位于 `~/.pepsi-code/settings.json`：

```json
{
  "model": "your-model-name",
  "env": {
    "ANTHROPIC_BASE_URL": "https://your-provider.example/anthropic",
    "ANTHROPIC_API_KEY": "your-api-key"
  }
}
```

也可使用环境变量：

| 变量 | 说明 |
| --- | --- |
| `PEPSI_CODE_MODEL` | 模型名称（最高优先级，覆盖配置文件） |
| `ANTHROPIC_MODEL` | 模型名称（被 `PEPSI_CODE_MODEL` 和 `settings.json` 覆盖） |
| `ANTHROPIC_API_KEY` | API Key |
| `ANTHROPIC_AUTH_TOKEN` | 可替代 API Key 的认证令牌 |
| `ANTHROPIC_BASE_URL` | Anthropic 兼容接口地址 |
| `PEPSI_CODE_MODEL_MODE=mock` | 使用 mock 模型 |
| `PEPSI_CODE_HOOKS=0` | 完全关闭 Hooks v2，不加载配置也不提示信任 |

使用下面的命令检查配置：

```bash
python -m pepsicode.main --validate-config
```

## 常用命令

| 命令 | 用途 |
| --- | --- |
| `/help` | 查看命令帮助 |
| `/tools` | 列出工具 |
| `/skills` | 列出 Skills |
| `/mcp` | 查看 MCP 状态 |
| `/plan [task]` | 进入只读 Plan 模式 |
| `/hooks` | 查看 Hooks v2 状态 |
| `/context` | 查看上下文占用 |
| `/cost` | 查看模型调用成本 |
| `/tasks` | 查看后台任务 |
| `/resume [id]` | 恢复会话 |
| `/worktree` | 管理 Git worktree |
| `/exit` | 退出并保存会话 |

## Plan 模式

输入 `/plan` 或 `/plan <任务>` 后，pepsicode 进入只读规划状态：

- 只允许只读工具、`ask_user`、`exit_plan_mode` 以及 explore/plan 子代理。
- 仅当前 `.pepsi-code/plans/` 中的计划文件允许写入。
- 模型调研并写出计划，用户批准后才退出 Plan 模式。
- Hooks v2 的 `command` 动作无条件禁用。
- `/hooks trust` 等会写入持久状态的入口也会被拒绝。

这意味着 Hook 可以为规划补充提醒或模型上下文，但不能借 Hook 运行命令绕过 Plan 边界。

## Hooks v2

### 配置层级

按以下顺序加载，同 ID 的可信后层配置覆盖前层配置：

1. `~/.pepsi-code/hooks.json`：用户级，自动视为可信。
2. `<workspace>/.pepsi-code/hooks.json`：项目级，适合提交到仓库。
3. `<workspace>/.pepsi-code/hooks.local.json`：本机覆盖，默认已加入 `.gitignore`。

项目级和本机 Hook 默认不执行。首次发现时，交互模式会提供“信任当前版本”“仅本次信任”“保持禁用”三个选择；非交互模式保持禁用。永久信任记录绑定到路径和文件内容 SHA-256 指纹，配置一旦变化就必须重新确认。

### 最小配置

```json
{
  "version": 1,
  "hooks": [
    {
      "id": "protect-secrets",
      "event": "pre_tool_use",
      "priority": 10,
      "when": {
        "tool": ["write_file", "edit_file", "patch_file"],
        "args.path": {"regex": "(^|[/\\\\])\\.env($|[/\\\\])"}
      },
      "action": {
        "type": "deny",
        "message": "拒绝修改受保护的环境配置：{path}"
      }
    },
    {
      "id": "review-after-tests",
      "event": "post_tool_use",
      "when": {
        "tool": "run_command",
        "args.command": {"contains": "pytest"}
      },
      "action": {
        "type": "context",
        "message": "测试命令已结束。检查失败原因和回归风险后再继续。"
      }
    },
    {
      "id": "notify-session-save",
      "event": "session_save",
      "action": {
        "type": "notify",
        "message": "会话 {session_id} 已保存"
      }
    }
  ]
}
```

配置根节点也可直接写成 Hook 数组。设置 `"strict": true` 后，只要同一文件存在一条无效配置，整份文件都不会加载。

### 支持的事件

| 事件 | 触发时机 |
| --- | --- |
| `pre_tool_use` | 工具校验后、权限检查和实际执行前 |
| `post_tool_use` | 工具执行完成后 |
| `agent_start` / `agent_stop` | 主代理回合开始和结束 |
| `subagent_start` / `subagent_stop` | 子代理开始和结束 |
| `user_input` | 收到用户输入 |
| `assistant_output` | 代理产出最终回复 |
| `context_compact` | 自动或溢出恢复压缩完成 |
| `session_save` / `session_resume` | 会话保存和恢复 |
| `startup` / `shutdown` | 应用启动和关闭 |
| `error` | 工具或模型流程发生错误 |

### 动作

`deny`

- 仅允许用于 `pre_tool_use`。
- 在权限检查和工具执行前同步阻断，并向模型返回明确错误。

`notify`

- 向 CLI/TTY 输出通知，不修改模型上下文。

`context`

- 将受长度限制的 `<hook-context>` 块加入下一次模型调用。
- 注入内容不会追加到会话消息，因此不会在多轮中重复积累。

`command`

- 使用 argv 数组，不通过字符串 shell 拼接。
- 复用 `run_command` 的目录边界、命令分类和权限审批。
- 不能用于 `pre_tool_use`，Plan 模式中始终禁用。
- 支持 `timeoutSeconds`、`background` 和 `exposeOutputToModel`。
- 大输出会写入 `.pepsi-code/tool-results/`，模型和 UI 只接收有界预览。

示例：

```json
{
  "id": "format-after-write",
  "event": "post_tool_use",
  "scope": ["main"],
  "when": {
    "tool": ["write_file", "edit_file", "patch_file"],
    "args.path": {"glob": "*.py"}
  },
  "action": {
    "type": "command",
    "argv": ["python", "-m", "ruff", "format", "{path}"],
    "timeoutSeconds": 30,
    "exposeOutputToModel": true
  }
}
```

命令是否获准仍由当前 PermissionManager 决定。Hook 配置本身不能声明“免审批”。

### 条件与模板

`when` 支持以下结构化操作符：

- 直接值：`eq`
- 数组：`in`
- `{ "contains": "..." }`
- `{ "glob": "..." }`
- `{ "regex": "..." }`

常用字段包括 `event`、`tool`、`path`、`args.<name>`、`result.<name>`、`permission_mode`、`agent_scope` 和错误字段。正则表达式和参与匹配的值都有长度限制，避免配置造成无界处理。

消息与 argv 支持 `{event}`、`{tool}`、`{path}`、`{cwd}`、`{permission_mode}`、`{agent_scope}`、`{error}` 和 `{args.<name>}` 替换。

### Scope、顺序与一次性执行

- `scope` 可为 `main`、`subagent` 或两者数组，默认两者都执行。
- `priority` 越小越先执行，默认 `100`。
- `once: true` 在单进程中最多执行一次；并行工具调用下也使用原子 claim。
- `background: true` 只适用于非前置动作，后台任务由受管线程池执行。
- Hook 失败不会让代理循环崩溃；`onError` 可设为 `ignore`、`warn`，前置 Hook 还可用 `deny`。

### 管理命令

```text
/hooks
/hooks list
/hooks errors
/hooks reload
/hooks trust
/hooks enable <id>
/hooks disable <id>
```

`/hooks enable` 和 `/hooks disable` 只影响当前进程。`/hooks trust` 会为当前项目 Hook 文件保存内容指纹，并重新加载配置。

### Python 兼容 API

旧版程序化事件 API 保持可用：

```python
from pepsicode.hooks import HookEvent, register_hook

unregister = register_hook(
    HookEvent.POST_TOOL_USE,
    lambda context: print(context.tool_name),
    "audit tool usage",
)
```

`HookManager`、`register_hook`、`fire_hook`、`fire_hook_sync`、`create_logging_hook` 和 `create_script_hook` 均保留。配置驱动的新 Hook 建议优先使用 `hooks.json`，因为它具备验证、信任和权限治理。

## 上下文管理

pepsicode 使用分层上下文控制：

1. 对较旧的大型工具结果保留头尾并截断中间部分。
2. 大工具输出持久化为上下文 artifact，只把有界预览传给模型。
3. 接近模型上下文上限时自动压缩旧消息。
4. API 返回上下文溢出时强制压缩并有限重试，压缩无进展时触发断路保护。

Hooks v2 的 `context` 动作沿用同样的有界原则：单条和单批注入都有上限，且只存活到下一次模型调用。

## 开发与验证

```bash
python -m ruff check pepsicode tests
python -m pytest -q
```

Hooks v2 专项测试：

```bash
python -m pytest -q tests/test_hooks_v2.py
```

主要目录：

```text
pepsicode/
  agent_loop.py          主代理同步/流式循环
  server.py              WebSocket 服务端（供桌面客户端连接）
  protocol.py            JSON-RPC 消息信封定义
  permissions.py         权限与 Plan 模式边界
  context_manager.py     上下文压缩与恢复
  hooks/                 Hooks v2 模型、加载、信任、引擎与运行时接线
  tools/                 内置工具
  agents/                子代理定义与追踪
  tui/                   终端界面
pepsicode-client/        Electron 桌面客户端
  electron/              主进程（main.ts）与预加载（preload.ts）
  src/                   React 前端源码
    conversation/        聊天界面组件
    sidebar/             会话列表与项目选择
    stores/              Zustand 状态管理（session / connection）
    theme/               白色主题设计令牌
tests/
```

## 安全说明

- 请在提交项目 Hook 前进行代码审查；项目 Hook 的信任是安全边界，不是形式提示。
- 不要把密钥写入 `hooks.json`、README 或仓库。
- `command` Hook 不会绕过命令权限，也不会在 Plan 模式执行。
- 后台 Hook 任务在进程关闭时会等待有限时间，未完成任务会被取消。
- `.pepsi-code/hooks.local.json` 适合本机差异，不应提交。

## License

MIT，见 [LICENSE](LICENSE)。
