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

界面顶部左侧显示当前模型与思考深度。点击可从当前 API 返回的模型列表中选择，显示名称与请求 ID 分别读取；思考档位来自接口的能力元数据。可点击「刷新模型列表」重新读取。点击「应用」后从下一轮生效，保留聊天记录与费用统计；当前轮执行期间禁止切换。

默认只修改当前会话，设置随下一轮对话一同自动保存，恢复会话时会恢复服务商、模型与思考深度。勾选「同时保存为新会话默认设置」会写入 `~/.pepsi-code/settings.json`；环境变量依然具有更高优先级。在 `providers` 中配置四家的独立凭据后，可在同一对话中切换服务商。Mock 模式下该入口禁用。

### 打包为独立 exe

```bash
cd pepsicode-client
npm run electron:build    # 输出到 release/ 目录
```

打包后的应用内嵌 Python 解释器，可直接分发给未安装 Python 环境的用户（需配合 PyInstaller 打包 `pepsicode` 包，具体见打包脚本）。

## 模型配置

当前提供 **DeepSeek（默认）、Kimi、GLM（智谱）、千问（百炼）** 四家预设，以及一个可命名的 **自定义厂商** 配置。各家分别使用独立的 API Key。终端与桌面端共用 `~/.pepsi-code/settings.json`，可参考仓库的 [`settings.providers.example.json`](settings.providers.example.json)：

终端输入 **`/provider`** 会列出全部四家预设（未配置 Key 的也会显示），默认选中 DeepSeek；选择尚未配置的预设（或直接输入 `/provider deepseek` 等）会打开该厂商的引导，地址已预填，从 API Key 开始填写。没有指定 `provider` 时，已配置的 DeepSeek 优先作为新会话服务商。

无需手写配置即可添加自定义厂商：终端输入 **`/provider add`**，或在 `/provider` 菜单中选择「+ 自定义厂商」；桌面端点击顶部模型按钮，再点「+ 添加自定义厂商」。尚未配置 API 时也能打开引导。当前接口格式为 **Anthropic Messages 兼容**，引导依次填写厂商名称、API Base URL、鉴权方式、API Key、可选的模型列表地址，然后选择模型、检查摘要并确认「保存并使用」。

鉴权支持 `Authorization: Bearer` 和 `x-api-key` 两种方式，Key 输入遮罩且不写入聊天记录或终端输入历史。模型列表直接从接口读取；未提供列表时，可明确选择手动填写服务商给出的模型 ID，并标注未验证可用性。确认前取消不会写入配置或修改当前服务商。终端用 Enter 前进、Alt+← 返回、Esc 取消；模型列表地址步骤可按 Tab 切换「无列表」。最后确认会将凭据写入本机 `~/.pepsi-code/settings.json`，立即启用并设为新会话默认；已有自定义厂商会被更新，其他厂商配置保留。

```json
{
  "provider": "deepseek",
  "providers": {
    "kimi": { "apiKeyEnv": "MOONSHOT_API_KEY" },
    "glm": { "apiKeyEnv": "ZHIPU_API_KEY" },
    "deepseek": { "apiKeyEnv": "DEEPSEEK_API_KEY" },
    "qwen": { "apiKeyEnv": "DASHSCOPE_API_KEY", "modelsUrl": false, "models": [] }
  }
}
```

在 PowerShell 中设置各家的凭据后启动：

```powershell
$env:MOONSHOT_API_KEY = "你的 Kimi Key"
$env:ZHIPU_API_KEY = "你的智谱 Key"
$env:DEEPSEEK_API_KEY = "你的 DeepSeek Key"
$env:DASHSCOPE_API_KEY = "你的百炼 Key"
pepsicode
```

也可在各家配置对象中使用 `apiKey` 或 `authToken`。未配置 Key 的服务商不能切换，不会借用其他家的凭据。`provider` 决定新会话启动的服务商，`PEPSI_CODE_PROVIDER` 可覆盖它。启用 `providers` 后不再继承 `~/.claude/settings.json` 中的模型与思考设置；每家可配置自己的 `model`、`models`、`modelsUrl`、`reasoningEffort`、`maxOutputTokens` 和 `fallbackModel`。

| 服务商 ID | 默认 Messages API Base URL | 接入说明 |
| --- | --- | --- |
| `kimi` | `https://api.moonshot.cn/anthropic` | [官方 Messages API](https://platform.kimi.com/docs/api/messages)；模型目录可设为同源 `/v1/models` |
| `glm` | `https://open.bigmodel.cn/api/anthropic` | [官方接入说明](https://github.com/MetaGLM/glm-cc/blob/main/glm-4.5-claude-code-integration.md)；套餐与标准 API 的目录权限以账号为准 |
| `deepseek` | `https://api.deepseek.com/anthropic` | [官方兼容接口](https://api-docs.deepseek.com/guides/anthropic_api/)；模型目录支持名称、ID、思考档位元数据 |
| `qwen` | `https://dashscope.aliyuncs.com/apps/anthropic` | [百炼接口说明](https://help.aliyun.com/zh/model-studio/anthropic-api-messages)；可覆盖为业务空间专属 Base URL |

**模型列表与思考档位不写死。** `/provider` 打开服务商菜单，再选择该服务商的模型，确认前不会修改当前服务商；Esc 可取消。桌面端点击顶部模型按钮后选择服务商和模型，再应用。切换会同步替换地址、凭据、模型目录和思考设置；会话保存服务商 ID、模型 ID 与档位，恢复时从本地配置重新取 Key。已有聊天记录保留，旧服务商的思考签名会清除。

各家的目录接口并不一致：百炼 Anthropic 兼容端点明确不提供 `/models`，示例因此用 `modelsUrl: false` 关闭探测，需将官方控制台确认的模型 ID 填入 `providers.qwen.models`。若账号提供同源目录地址，也可设置 `modelsUrl` 自动读取。Kimi 的模型目录返回能力标记和上下文长度，但未声明的思考档位不会自动补全；可按官方参数文档在对应 `models` 项中显式声明能力。`output_effort` 模式只发送 `output_config.effort`，适合不接受 `thinking` 开关的兼容端点。声明与版本相关的能力时应使用准确的模型 ID，不做名称前缀匹配。

下面的单接口配置仍可使用：

用户配置文件位于 `~/.pepsi-code/settings.json`：

```json
{
  "model": "your-provider-model-id",
  "reasoningEffort": "default",
  "env": {
    "ANTHROPIC_BASE_URL": "https://your-provider.example/anthropic",
    "ANTHROPIC_API_KEY": "your-api-key"
  }
}
```

启动时使用当前 API 地址与凭据读取模型列表，菜单显示接口返回的 `name` / `display_name`，请求使用对应的 `id`，不内置模型候选名称。列表接口通常为 `/v1/models` 或 `/models`，会保留兼容网关的路径前缀并读取分页；若网关采用其他路径，可在配置中设置同源的 `modelsUrl`。列表缓存五分钟，终端可用 `/model refresh` 或菜单中的 `r` 强制刷新。

`reasoningEffort` 默认为 `default`，不额外发送思考参数。其余档位读取接口返回的 `effort.supported_levels` 或 `capabilities.effort`，保留服务端新增的档位；只有接口明确声明支持时才显示「关闭思考」。接口未提供能力元数据时只显示默认档位，不根据模型名称推测。

读取失败时显示具体状态并保留上次成功的列表；首次读取失败则仅显示本地配置，并标记未验证可用性。对于未提供能力元数据的网关，可按其文档显式配置 `models`，作为缺失元数据的补充：

```json
{
  "model": "my-model-alias",
  "reasoningEffort": "high",
  "models": [
    {
      "id": "my-model-alias",
      "reasoningMode": "effort",
      "reasoningLevels": ["low", "high", "max"],
      "supportsThinkingOff": true
    }
  ]
}
```

`reasoningMode` 可设为 `none`（默认参数）、`budget`（`thinking.budget_tokens`）、`budget_effort`（预算与 `output_config.effort`）、`adaptive`（自适应 `thinking` + `output_config.effort`）、`output_effort`（只发送 `output_config.effort`）或 `effort`（启用 `thinking` + `output_config.effort`）。显式使用 `budget` 模式时，客户端的 `low/medium/high` 预算分别为 1024/4096/16384；`maxOutputTokens` 必须大于预算，未配置时预留 4096 token 用于回答。能力声明应与服务端实际支持保持一致，参考 [DeepSeek 参数说明](https://api-docs.deepseek.com/guides/anthropic_api/) 和 [Kimi 参数参考](https://platform.kimi.com/docs/api/messages)。默认请求显式发送 `max_tokens`，未配置输出上限时使用客户端的 4096 token 默认值（不会超过接口声明的上限）。

也可使用环境变量：

| 变量 | 说明 |
| --- | --- |
| `PEPSI_CODE_MODEL` | 模型名称（最高优先级，覆盖配置文件） |
| `PEPSI_CODE_PROVIDER` | 新会话服务商：`kimi`、`glm`、`deepseek`、`qwen`、`custom` |
| `PEPSI_CODE_REASONING_EFFORT` | 思考深度，覆盖配置文件的 `reasoningEffort`；交互切换可覆盖当前会话 |
| `ANTHROPIC_MODEL` | 模型名称（被 `PEPSI_CODE_MODEL` 和 `settings.json` 覆盖） |
| `ANTHROPIC_API_KEY` | API Key |
| `ANTHROPIC_AUTH_TOKEN` | 可替代 API Key 的认证令牌 |
| `ANTHROPIC_BASE_URL` | Anthropic 兼容接口地址 |
| `PEPSI_CODE_MODEL_MODE=mock` | 使用 mock 模型 |
| `PEPSI_CODE_MOUSE=0` | 关闭鼠标捕获，改用终端原生选择；默认开启，支持滚轮翻看历史和应用内拖选复制 |
| `PEPSI_CODE_HOOKS=0` | 完全关闭 Hooks v2，不加载配置也不提示信任 |
| `NO_COLOR=1` | 关闭终端配色，保留交互光标和文字强调 |

终端界面使用统一的暖色强调、紧凑启动页和固定位置的输入区。命令菜单会按窗口高度缩短，并保持当前选中项可见；中文和长路径按实际显示宽度换行。输出重定向到文件或 `TERM=dumb` 时自动使用无样式文本。

交互界面只刷新发生变化的行，并用同步输出（DEC 2026）一次性提交整帧，流式刷新最多每秒 30 次；工具结果直接以折叠状态出现，权限确认在输入区原位显示，不会整屏切换。长输入会随光标横向滚动；窗口调整大小后会重新排版，并预留最右侧一列以避免终端自动折行造成页面跳动。

终端默认捕获鼠标：滚轮或 `PageUp` / `PageDown` 翻看聊天记录，翻到上方时新输出不会把视图拉回底部。用鼠标左键拖选文字，松开后自动复制到剪贴板（Windows 原生剪贴板、`pbcopy`、`wl-copy`/`xclip`/`xsel`，都不可用时使用 OSC 52）；选中状态下连续按 `Ctrl+C` 都只复制并保留选区，按任意其他键取消选中。没有可识别选区时，第一次 `Ctrl+C` 只显示提示，需在两秒内再次按下才退出；也可以输入 `/exit` 后按 Enter 退出。如果更想用终端原生选择，大多数终端（包括 Windows Terminal）可按住 `Shift` 拖选（参见 [文字选择](https://learn.microsoft.com/en-us/windows/terminal/selection)），或在启动前设置 `PEPSI_CODE_MOUSE=0` 完全关闭鼠标捕获。Windows 交互期间关闭控制台对 `Ctrl+C` 的系统中断处理，退出后恢复原始输入模式。

使用下面的命令检查配置：

```bash
python -m pepsicode.main --validate-config
```

终端交互界面和管道输入都支持在当前进程中切换模型、调整思考深度，无需重启：

在交互终端输入 `/model` 或 `/effort` 并回车，会打开选择菜单。思考强度菜单列出当前模型由接口声明的全部档位，常见档位附中文说明，不需要手动填写参数；`√ 当前` 标记已生效的选项，`>` 表示方向键正在选中的选项。用 ↑/↓ 移动、Enter 应用、Esc 取消，按 `s` 切换是否保存为新会话默认设置，按 `r` 重新读取接口。

如果配置的模型 ID 不在当前接口返回的列表中，交互界面会显示配置来源并打开模型菜单；选择前不发送模型请求。尚未发送的问题会保留在输入框中。下面的带参数命令仍可用于快捷操作和管道输入（模型 ID 与档位以接口返回内容为准）：

```text
/model                         # 查看当前模型与思考深度
/provider                      # 打开服务商菜单，再选择模型
/provider add                  # 逐步配置自定义厂商（交互终端）
/provider glm                  # 打开智谱的模型菜单（交互终端）
/provider kimi <model-id>      # 切换服务商及模型，支持 --save
/model list                    # 查看模型候选
/model refresh                 # 强制从当前 API 重新读取模型列表
/model <provider-model-id>     # 切换当前会话模型
/effort                        # 查看当前深度及该模型允许的档位
/effort max                    # 调整深度，从下一轮生效
/effort off                    # 关闭思考（需模型支持）
/effort default                # 恢复服务端默认行为
/effort high --save            # 同时把当前模型与深度保存为新会话默认设置
/model <provider-model-id> --save # 切换模型并保存默认设置
```

默认只影响当前会话，保留聊天记录；切换到其他模型时将思考深度重置为 `default`，可再用 `/effort` 选择。终端输入区显示当前模型和深度，交互会话在自动保存或退出时保存设置，`--resume` 和 `/resume` 会恢复它们。管道模式的设置仅在当前进程有效，持久化请使用 `--save`。环境变量会在新进程启动时覆盖已保存的默认设置。当前轮执行期间、Mock 模式下不允许切换；Plan 模式可查询设置和候选列表，但禁止修改。

## 常用命令

| 命令 | 用途 |
| --- | --- |
| `/help` | 查看命令帮助 |
| `/model [list\|refresh\|模型 ID] [--save]` | 查看、刷新或切换模型；可选保存默认设置 |
| `/provider [服务商 ID] [模型 ID] [--save]` | 选择服务商与模型；`list` 查看配置状态 |
| `/effort [档位] [--save]` | 查看或调整当前模型的思考深度 |
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

## 项目级 Skill 与 MCP 示例

仓库内置了一个可以直接验证扩展链路的示例：

- `.pepsi-code/skills/pepsicode-project-health/SKILL.md`：项目健康检查 Skill。
- `examples/mcp/workspace_inspector_server.py`：零外部依赖、只读的 stdio MCP 服务。
- `.mcp.json` 中的 `workspace-inspector`：项目级 MCP 连接配置。
- `.mcp.json` 中的 `openai-developer-docs`：OpenAI 官方公开、只读的远程 Streamable HTTP MCP。

启动 Pepsicode 后运行 `/skills` 和 `/mcp` 应看到 `pepsicode-project-health`、本地 `workspace-inspector` 和远程 `openai-developer-docs`。在对话中要求“使用 pepsicode-project-health 检查项目”，Skill 会优先调用本地 MCP 的 `project_info` 工具获取仓库元数据；询问 OpenAI API、Codex 或插件问题时，可以调用远程文档 MCP 搜索官方资料。

也可以不启动模型，直接运行定向集成测试：

```bash
python -m pytest -q tests/test_project_extensions.py
```

## 权限模式

点击桌面输入框左下角的「权限」，或输入 `/permissions` 打开三选一菜单，当前模式后显示 `√`。终端中用 ↑/↓ 选择、Enter 应用、Esc 返回，无需填写参数。带参数命令仍保留供脚本使用。

| 模式 | 行为 |
| --- | --- |
| 只读 | 允许文件读取、搜索和声明为只读的工具；拒绝编辑、命令执行及外部写入 |
| 自动（新会话默认） | 项目内编辑和常规命令免确认；项目外路径、危险或未知命令、未声明只读的 MCP 工具按需确认 |
| 完全访问 | 文件访问、编辑、命令及 MCP 工具均免权限确认 |

模式随会话保存，恢复会话时恢复选择。Plan 是独立的规划状态，始终保留只读边界和计划审批，退出后回到所选权限模式。完全访问也不会取消 Hook 的拒绝规则。

自动模式是应用层审批策略，当前命令后端没有操作系统级沙箱；运行的程序仍具有当前系统用户的权限。Python、Shell 等任意代码执行命令仍按现有规则确认。

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

终端输入区和桌面输入框下方常驻显示「缓存命中率」与「当前上下文窗口占比」，收到流式 `message_start` / `message_delta` 的用量数据时立即更新；工具调用之间也会刷新，无需手动输入 `/context`。

缓存命中率按最近一次模型请求的缓存读取 token ÷ 总输入 token 计算（总输入包含普通输入、缓存创建和缓存读取，不包含输出）。支持 Anthropic 缓存字段及兼容接口返回的缓存命中字段；接口未返回缓存统计时显示「未提供」，明确返回零命中时才显示 `0%`。上下文占比包含缓存输入，容量优先使用接口或本地模型配置的 `contextWindow`；使用默认容量或估算 token 数时标注「约」。桌面端悬停可查看 token 数和容量。

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
  main.py                CLI 入口
  config.py              配置加载
  tooling.py             工具协议与注册表
  permissions.py         权限与 Plan 模式边界
  core/                  代理引擎：agent_loop（主循环）、session、state、任务编排
  llm/                   模型层：anthropic_adapter、mock_model、重试与成本统计
  context/               上下文层：压缩恢复、记忆系统、系统提示词、skills
  cli/                   命令层：斜杠命令、管理 CLI、安装器
  server/                WebSocket 服务端（python -m pepsicode.server）与协议定义
  hooks/                 Hooks v2 模型、加载、信任、引擎与运行时接线
  tools/                 内置工具
  agents/                子代理定义与追踪
  approval/              审批后端
  sandbox/               命令沙箱分类
  worktree/              Git worktree 会话管理
  tui/                   终端界面（app.py 为全屏 TUI 主程序）
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
