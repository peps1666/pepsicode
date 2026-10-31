# 模型、上下文与配置

> 服务商、模型、思考档位、上下文管理和环境变量。

[← 返回 README](../README.md)

## PepsiCode 内置 Harness 准则

`pepsicode/prompts/AGENTS.md` 定义 PepsiCode 在所有项目中的通用行为，包括任务处理、
工具使用、权限、验证、沟通和记忆策略。程序从自身安装包读取该文件，作为主会话和
子代理的系统指令；子代理另加角色与工具限制。它随 Python 安装包和桌面后端一起分发，
不依赖启动目录，也不会用用户项目里的同名文件替换。资源缺失、空白、编码错误或
超过 32 KiB 会明确报错。桌面后端的 `--self-test` 同时检查此资源。

修改内置准则后重启程序或新建会话生效；旧版会话恢复时会重建系统提示词。
权限状态、可用工具、项目规则和任务状态仍由程序动态组装，实际执行限制由代码保证。
仓库根目录的 `AGENTS.md` 是开发 PepsiCode 的项目规则，与这份内置准则分别维护。

## 项目指令（AGENTS.md）

将项目的编码约定、架构限制和验证命令写入项目根目录的 `AGENTS.md`。
打开项目后，PepsiCode 在每次模型请求前自动读取指令并加入上下文；
终端、桌面端、QQ 通道和子代理共用这条加载路径，无需手动粘贴。

加载顺序为可选的 `~/.pepsi-code/AGENTS.md`，再从最近的 Git 根目录到
会话当前工作目录逐层读取 `AGENTS.md`，更具体目录的规则优先。
Git worktree 的 `.git` 文件同样标识根目录；非 Git 项目只读取当前目录，
不向上加载其他目录的项目规则，也不递归扫描尚未进入的子目录。

文件修改、删除和切换项目会在下一次模型请求时反映到上下文中；
相同指令不会在每次请求时重复追加，变化通过带来源路径的内部消息保存。
空文件和缺失文件跳过，读取或 UTF-8 解码失败会在指令上下文中说明。
每次加载的文件正文合计最多 32 KiB，按加载顺序使用预算，超出时明确标注截断和遗漏。
项目指令不能改变宿主权限或解除 Plan 模式限制。

`CLAUDE.md` 不再作为项目或全局指令自动读取；原有规则需迁移至对应的
`AGENTS.md`。此变更不影响模型配置或 Skills 的独立兼容设置。

## 模型配置

当前提供 **DeepSeek（默认）、Kimi、GLM（智谱）、千问（百炼）** 四家预设，以及一个可命名的 **自定义厂商** 配置。各家分别使用独立的 API Key。终端与桌面端共用 `~/.pepsi-code/settings.json`，可参考仓库的 [`settings.providers.example.json`](../settings.providers.example.json)：

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

下面的单接口配置仍可使用，用户配置文件位于 `~/.pepsi-code/settings.json`：

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

读取失败时显示具体状态并保留上次成功的列表；首次读取失败则仅显示本地配置，并标记未验证可用性。对于代理或自建网关，可按其实际配置显式设置模型能力：

```json
{
  "model": "my-model-alias",
  "reasoningEffort": "high",
  "models": [
    {
      "id": "my-model-alias",
      "contextWindow": 128000,
      "maxOutputTokens": 8192,
      "reasoningMode": "effort",
      "reasoningLevels": ["low", "high", "max"],
      "supportsThinkingOff": true
    }
  ]
}
```

`reasoningMode` 可设为 `none`（默认参数）、`budget`（`thinking.budget_tokens`）、`budget_effort`（预算与 `output_config.effort`）、`adaptive`（自适应 `thinking` + `output_config.effort`）、`output_effort`（只发送 `output_config.effort`）或 `effort`（启用 `thinking` + `output_config.effort`）。显式使用 `budget` 模式时，客户端的 `low/medium/high` 预算分别为 1024/4096/16384，输出上限必须大于思考预算。能力声明应与服务端实际支持保持一致，参考 [DeepSeek 参数说明](https://api-docs.deepseek.com/guides/anthropic_api/) 和 [Kimi 参数参考](https://platform.kimi.com/docs/api/messages)。

模型列表返回 `context_window` / `context_length` 和 `max_output_tokens` 时，自动随模型切换使用这些能力。默认单次输出预算取模型最大输出和 65536 两者的较小值，避免把模型的整个上限用于每次请求；未提供最大输出时回退到 8192，显式思考预算较大时额外预留回答空间。如果接口没有提供上限而实际支持不足 8192，需按服务商文档手动设置。可以在服务商配置中设定 `maxOutputTokens` 或使用 `PEPSI_CODE_MAX_OUTPUT_TOKENS` 覆盖；显式设置超过模型有效上限时会报错。终端模型菜单可查看接口提供的容量，管道模式下 `/model` 还显示基础输出预算。上下文窗口缺失时沿用本地默认或估算容量。

`models` 中的 `contextWindow` 和 `maxOutputTokens` 是对应模型在当前服务商上的实际限制，优先于接口返回值，适合代理或自建服务限制较低的情况。请求实际输出预算还会根据当前输入估算和剩余窗口缩小；输入占用接近窗口时自动压缩或提示无法安全压缩，估算仍可能与服务商计数不同。模型菜单标注 `config`（手动配置）、`api`（接口）、`last-api`（本次刷新失败后的上次接口数据）或 `cache`（磁盘缓存）。API 成功返回的限制会按接口及凭据身份隔离缓存在用户目录，最多保留 7 天；缓存只补充同一模型的能力，不证明模型当前可用。

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

任务运行时，没有选区的 `Ctrl+C` 会停止当前轮并保留会话界面；空闲时仍需在两秒内连续按两次退出。停止信号会传给模型读取、后续工具和子代理，命令会清理自身的子进程；尚在建立连接、未收到 HTTP 响应头的请求需要等待连接超时或返回后停止。`/cmd` 等工具快捷命令在后台执行，运行时可继续编辑草稿，按 Enter 会保留草稿而不会丢弃。支持括号粘贴的终端会把整段粘贴内容放入输入框，多行文本保留换行，需另按 Enter 发送。`Ctrl+A/E/U` 分别移到行首、行尾、清空输入，`Ctrl+P/N` 浏览输入历史。

自动保存会同步最新消息和终端记录，包括尚未完成的输出。`/resume` 仅列出当前项目的会话；使用 ID 恢复其他项目时会提示先切换到对应目录，再运行 `pepsicode --resume <id>`。

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

## 上下文管理

终端输入区和桌面输入框下方常驻显示「缓存命中率」与「当前上下文窗口占比」，收到流式 `message_start` / `message_delta` 的用量数据时立即更新；工具调用之间也会刷新，无需手动输入 `/context`。

缓存命中率按最近一次模型请求的缓存读取 token ÷ 总输入 token 计算（总输入包含普通输入、缓存创建和缓存读取，不包含输出）。支持 Anthropic 缓存字段及兼容接口返回的缓存命中字段；接口未返回缓存统计时显示「未提供」，明确返回零命中时才显示 `0%`。上下文占比包含缓存输入，容量优先使用接口或本地模型配置的 `contextWindow`；使用默认容量或估算 token 数时标注「约」。桌面端悬停可查看 token 数和容量。

pepsicode 使用分层上下文控制：

1. 对较旧的大型工具结果保留头尾并截断中间部分。
2. 大工具输出持久化为上下文 artifact，只把有界预览传给模型。
3. 接近模型上下文上限时自动压缩旧消息。
4. API 返回上下文溢出时强制压缩并有限重试，压缩无进展时触发断路保护。

Hooks v2 的 `context` 动作沿用同样的有界原则：单条和单批注入都有上限，且只存活到下一次模型调用。
