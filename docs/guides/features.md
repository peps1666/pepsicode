# 功能与工作流

> 完整能力清单：交互、代码理解、质量闭环、扩展、安全与上下文。

[← 返回 README](../../README.md)

## 一轮任务

| 阶段 | 做什么 | 把关点 |
| --- | --- | --- |
| **① 规划**（可选） | 进入 Plan 模式只读调研，写出计划等你批准 | 只读边界 · 计划审批 |
| **② 执行** | 按计划调工具改文件、跑命令 | 权限模式 · Hooks v2 |
| **③ 验证** | 按项目约定跑 tests / lint / build | 无测试、被拒绝都不算通过 |
| **④ 交付或继续修** | 通过则汇总交付；失败把输出送回模型继续修，超过阈值就停 | 无进展断路保护 |

## 功能一览

### 交互与协作

| 能力 | 说明 |
| --- | --- |
| 全屏 TUI | 只刷新变化的行，用 DEC 2026 同步输出整帧提交，流式刷新最多每秒 30 次；用户回合用一块浅底色卡片标出（无标签、无边框），回看长会话时更容易定位；权限确认在输入区原位显示，不整屏切换 |
| 桌面客户端 | React + Vite 白色主题，WebSocket + JSON-RPC 信封，流式事件与权限审批弹窗；主进程自动拉起 Python 服务端 |
| 运行中补充指令 | 任务不停也能继续输入：补充当前任务、排到完成后执行、随时取回，队列最多 32 条 |
| 对话分支 | 树形历史保存多条路线，`/tree` 切换、`/fork` 分叉为独立会话、`/clone` 复制、`/retry` 新分支重跑 |
| Markdown 自定义命令 | 把 `.md` 放进 `.pepsi-code/commands/` 就是一条命令，支持位置参数与默认值，排在队列里也生效 |
| QQ 私聊好友 Pepsi | 独立 OneBot v11 通道，支持管理员私聊、连续对话、任务排队、QQ 内审批和断线恢复；见 [QQ 接入指南](../../integrations/qq/README.md) |

### 代码理解与修改

| 能力 | 说明 |
| --- | --- |
| 按需语义导航 | 文件符号、定义跳转、引用、诊断与只读重命名预览；按需启动 basedpyright / pyright / pylsp、typescript-language-server、gopls、rust-analyzer，缺服务时明确标记 fallback |
| 跨文件重命名 | `rename_symbol` 逐文件经过路径与编辑审批，不套用 Python 标识符规则，由对应语言服务校验 |
| 编辑工具族 | 整文件写入、子串替换、多处精确替换、整文件覆盖、批量多文件编辑、Jupyter Notebook 单元格编辑，改动前先给 diff |
| 搜索与浏览 | 正则内容搜索、目录列举、带图标的文件树、有界读取，超大输出转为 artifact 后按 ID 分块读回 |
| 按需加载工具 | 延迟加载时，首个请求只发送核心工具定义，其余用 `search_tools` 查找 |

### 质量闭环

| 能力 | 说明 |
| --- | --- |
| 自动验证与修复 | `verificationMode: auto/always/off`；按真实测试文件与 `package.json` 脚本发现检查，失败进上下文、修复后重跑，无进展达上限即停 |
| 验证报告可追溯 | 终端显示进度，桌面显示可展开报告；无测试、被审批或 Hook 拒绝、未执行都不会被算作「通过」 |
| 测试与调试 | `test_runner` 支持 pytest / unittest 与覆盖率，`run_with_debug` 解析 Python traceback 与 Node 堆栈并回传结构化错误位置 |
| 代码审查与差异 | `code_review` 检查未用导入、硬编码、缺失文档与超长函数；`diff_viewer` 提供 unified / inline / stats 三种对比 |

### 扩展与自动化

| 能力 | 说明 |
| --- | --- |
| Hooks v2 | 12 类生命周期事件 × 4 种安全动作（`deny` / `notify` / `context` / `command`），关键前置 Hook 同步确定性阻断 |
| 项目信任 | 项目级 Hook 默认禁用，按路径与文件内容 SHA-256 指纹授权，配置一改就重新确认 |
| MCP | 本地 stdio 与远程 Streamable HTTP；可逐工具声明 `readOnlyTools`，未声明只读的工具按需审批 |
| Skills | `SKILL.md` 按需加载的领域说明；内置演示 Skill 走本地 MCP 取仓库元数据 |
| 子代理 | `explore` / `general` 两种主要角色，支持兼容旧名称及按需配置的自定义角色 |

### 安全与治理

| 能力 | 说明 |
| --- | --- |
| 权限模式 | 用 `/permissions` 选择每次询问、自动审批或完全信任，可为工作区设置新会话默认值 |
| Plan 只读边界 | 只允许只读工具与计划文件写入；Hook 的 `command` 动作在 Plan 中无条件禁用，无法借 Hook 绕过 |
| 命令审批 | 自动审批放行项目内常规命令，高风险命令需确认。默认本地后端以当前用户权限运行，详见[权限与安全](permissions.md) |
| 连接认证 | 建会话前必须令牌认证并校验 Origin；令牌只走受限 IPC，不进 URL 或日志 |
| 启动变量隔离 | 子进程继承厂商 Key 与模型设置，同时移除 Electron / Chromium 的运行时注入变量 |

### 上下文与可观测

| 能力 | 说明 |
| --- | --- |
| 常驻用量显示 | 输入区下方常驻「缓存命中率」与「上下文窗口占比」，随流式用量即时刷新 |
| 分层上下文控制 | 大结果头尾保留 → 大输出 artifact 化 → 完整分块摘要与旧 checkpoint 归并 → 溢出强制压缩并有限重试；摘要失败保留原上下文 |
| 成本与状态 | `/cost`、`/cost-report`、`/context`、`/status`、`/tasks` 随时查 |
| 会话持久化 | 自动保存最新消息与终端记录（含未完成输出），`/resume` 按项目列出并恢复 |
| 持久记忆 | `save_memory` / `search_memory` / `forget_memory`，分 `user` / `project` / `local` 三种作用域 |

> 语义导航、自动验证与工具按需加载的完整配置项见[代码理解与执行闭环](../architecture/agent-capabilities.md)。

## 内置工具

输入 `/tools` 查看当前会话的工具。默认加载策略按工具定义大小与模型上下文窗口选择一次加载或按需加载。按需加载时，用 `search_tools` 按名称或关键词查找，或用 `query: "*"` 和 `offset` 分页浏览。

| 分类 | 工具 |
| --- | --- |
| 文件与代码 | `read_file`、`write_file`、`edit_file`、`modify_file`、`patch_file`、`multi_edit`、`notebook_edit`、`read_context_artifact` |
| 搜索与浏览 | `grep_files`、`list_files`、`file_tree` |
| 代码智能 | `code_intelligence`、`find_symbols`、`find_references`、`get_ast_info`、`rename_symbol`、`code_review` |
| 执行与验证 | `run_command`、`run_with_debug`、`test_runner`、`governance_audit` |
| 协作与规划 | `task`、`task_state`、`agent_status`、`execution_status`、`todo_write`、`ask_user`、`exit_plan_mode`、`search_tools`、`load_skill` |
| 外部信息 | `web_search`、`web_fetch`、`api_tester` |
| 数据与服务 | `db_explorer`、`docker_helper` |
| 版本控制 | `git`、`worktree`、`diff_viewer` |
| 记忆 | `save_memory`、`search_memory`、`forget_memory` |

`web_search` 使用 Tavily，需在启动环境中设置 `TAVILY_API_KEY`；固定的基础搜索可在 Plan 和只读模式使用，并可能消耗 Tavily 额度。详细设置见[网页搜索](configuration.md#网页搜索)。

写入类工具（`write_file`、`edit_file`、`modify_file`、`patch_file`、`multi_edit`）都会先给出 diff 再落盘，审批规则由当前权限模式决定。可用 `toolCoreTools` 配置按需加载时的初始工具。

## 子代理与后台任务

`task` 把限定的子任务交给独立上下文窗口的子代理，主代理只收到摘要，因此大规模代码探索不会撑爆主上下文。

主代理可根据任务主动决定是否委派：相互独立或范围较大的调查适合 `explore`，边界清晰的独立实现可交给 `general`。简单任务和依赖当前上下文的步骤通常直接处理。用户也可以明确要求使用或不使用子代理。是否调用仍由模型结合任务判断，并非硬编码的自动触发规则。

| 子代理 | 定位 |
| --- | --- |
| `explore` | 快速只读搜索，定位文件与调用关系 |
| `general` | 在现有权限约束下进行读写和命令执行 |

`plan` 是兼容旧调用的 `explore` 别名；旧的 `verification` 名称仍可显式调用，执行命令仍需经过权限检查，不属于只读操作。独立审查可按需配置自定义 agent，必须显式列出 `allowedTools`。项目和用户定义不能覆盖内置名称，调用结果显示定义来源与有效工具。自定义定义修改后在下次调用时加载。

例如，在项目的 `.pepsi-code/agents/review.md` 添加只读审查角色：

```markdown
---
name: Review
isReadOnly: true
allowedTools:
  - read_file
  - list_files
  - grep_files
---
独立审查改动，报告可复现的问题、位置和剩余风险。
```

同一批次中连续的 `explore` / `plan` 调用可以并发，父轮次同步等待全部结果并按原顺序接收；包含命令或写入的任务不参与此并发。子任务持有父任务的只读投影，在后续步骤和执行入口检查父任务修订；发生变化后阻止继续执行并标记结果过期。已启动的外部操作不因此回滚。子代理报告不自动成为任务验收证据。

桌面端在「执行轨迹」查看 Agent 的负责事项、当前活动、最近报告、耗时和问题。默认 TUI 在子 Agent 出现后自动显示右侧状态栏，当前执行优先，结束后保留最近结果；点击名称查看详情，记录较多时点击底部翻页。终端宽度不足 110 列或可用高度不足时，改为输入框上方的简短状态区，不必反复输入命令。终端仍可用 `/agents [执行 ID 或分页偏移]` 查询完整编号和问题，QQ 私聊也支持 `/agents`。桌面问题卡片直接回复。终端和 QQ 使用 `/answer <问题 ID> <回复>`，可逐一回答同一任务的多个问题。

Agent 列表记录通过 `task` 委派的 Worker。主代理的状态与验收仍显示在任务轨迹。

主代理可用 `agent_status` 分页查询执行记录，或传入 `trace_id` 查看单次执行详情。Worker 用 `agent_report` 报告阶段变化、结果、阻碍或问题。自定义 Worker 如果设置了 `allowedTools`，需在列表中加入 `agent_report` 才能使用。必答问题结束当前轮次并等待用户。可选问题允许 Worker 继续检查。回复后由主代理决定是否重新委派，每次委派生成独立的执行 ID。

Worker 的 `ready` 先显示为结果待核验；Worker 明确报告 `failed`/`blocked` 时按实际结果结算为失败或阻塞，协调者收到对应事件而不是待核验结果。父任务完成且当前版本的有效证据覆盖全部验收条件后，显示父任务验收通过；已交付集成的会话按实际交付版本检查验收条件。旧修订的执行仍可查询。重启后，先前未结束的 Worker 显示为中断待核对。保存失败的提示在下一次会话成功提交后清除。

子 Agent 的 `maxTurns` 限制模型交互轮数，一轮可以调用多个工具。内核在请求中提供剩余轮数，临近上限时提醒收尾；此提醒不会增加执行预算，也不保证模型遵从。达到轮数上限时归为执行限制，保留已提交的报告并交给主 Agent，报告仍需核验。终端将其显示为“达到轮数上限”，旧记录中的同类失败也按此原因展示，避免误解成代码执行出错。

子任务结果的工具事件包含状态、定义来源、实际工具、执行 ID 和父任务修订等结构化字段；模型收到简短文本。子任务工具取父注册表与子任务允许工具的交集。子任务有独立请求次数上限，同时消耗父任务共享预算。子任务审批提示显示角色及执行 ID。

子代理与主代理共用同一套 Hook 引擎，各自保留 `scope`（`main` / `subagent`）。运行中的轮次可以随时停止，停止信号会传给模型读取、后续工具与子代理，命令会清理自身子进程。`/tasks` 查看后台任务。

## 记忆系统

`save_memory` 持久化决策、约定、模式与事实，跨会话保留，按作用域分开存放：

| 作用域 | 用途 |
| --- | --- |
| `user` | 跨项目通用偏好 |
| `project` | 可随仓库共享、纳入版本管理 |
| `local` | 本项目专用，不提交 |

传入已有 `id` 会替换该条目而不是追加。`search_memory` 按内容、类别或标签查找不在当前提示词里的条目，`forget_memory` 按作用域与 `id` 删除——不要留下互相矛盾的两条。终端用 `/mem` 或 `/memory` 查看。

## 运行中补充指令与任务排队

任务运行时可以继续输入，不必先停止代理：

| 操作 | 桌面端 | 终端 |
| --- | --- | --- |
| 补充当前任务 | 选择「补充当前任务」并发送 | Enter |
| 完成后执行新任务 | 选择「完成后执行」，或 Alt+Enter | Alt+Enter，或输入 `/queue 任务内容` |
| 取回尚未处理的输入 | 点击队列中的「取回」 | Alt+↑ 取回最后一条 |
| 停止任务 | 点击「停止」 | Ctrl+C |

补充指令在当前模型响应及整批工具执行完成后、下一次模型请求前生效，不会打断已开始的命令。后续任务按提交顺序逐项执行，等待当前任务的自动验证与修复结束；补充指令优先于后续任务。排队本身不改变权限或 Plan 模式。

停止、模型报错、验证失败或受阻、代理等待澄清时，尚未处理的输入退回编辑框，并保留正在编辑的草稿。只有实际进入代理上下文的输入才会加入聊天记录。运行时不能通过队列执行 `/model`、`/permissions` 等本地命令，请在任务结束后操作；下文 Markdown 提示词命令可以排队。队列最多容纳 32 条输入，原始输入最多 32000 字符，Markdown 命令展开后最多 128000 字符，与空闲时调用使用同一展开上限；队列只在当前进程中保留，尚未提供跨进程恢复。

部分 Windows 终端会占用 Alt 快捷键，可使用 `/queue` 和桌面端按钮。

## 对话分支与重新生成

对话以树形历史保存，可以从旧消息继续尝试另一套方案，原路线仍可切回。各分支共用当前工作目录，切换分支不会撤销文件，也不会创建 Git worktree。

| 命令 | 行为 |
| --- | --- |
| `/tree` | 显示当前会话所有路线及消息 ID，`+` 表示当前路线，`*` 表示当前末尾 |
| `/tree <id>` | 切换到所选消息；选用户消息会返回该消息之前，并取出原文作为草稿；选助手回答则从回答之后继续 |
| `/fork` | 列出可作为分叉起点的用户消息 |
| `/fork <用户消息id>` | 新建独立会话，复制起点之前的路线，并取出该用户消息；原会话完整保留 |
| `/clone` | 新建独立会话，复制当前路线全部内容 |
| `/retry` | 在当前会话的新分支重新执行上一条用户消息，保留原回答 |

消息 ID 可以使用唯一前缀。桌面端点击顶部「对话分支」可查看和切换路线；用户消息下方提供「从此分叉」「重新生成」。终端选择用户消息后会把原文放入编辑框；管道 CLI 会输出草稿文本，由下一行输入决定继续内容。

重新生成会再次调用模型，可能再次执行工具和修改文件，沿用当前模型、权限审批和自动验证。Markdown 命令使用原先保存的展开内容。运行中的任务结束后才能切换、分叉或重新生成。

分支历史随会话保存，压缩模型上下文不会删除已经归档的原始消息；切换分支会清空旧路线的压缩摘要。旧版本会话按已保存的内容导入，无法补回升级前已经被压缩删除的消息。文件状态、当前权限和待验证修改不随历史消息回退。

## Plan 模式

输入 `/plan` 或 `/plan <任务>` 后，pepsicode 进入只读规划状态：

- 只允许只读工具、`ask_user`、`exit_plan_mode` 以及有效工具集只读的子代理。
- 仅当前 `.pepsi-code/plans/` 中的计划文件允许写入。
- 模型调研并写出计划，通过审批后才会自动开始执行。
- Hooks v2 的 `command` 动作无条件禁用。
- `/hooks trust` 等会写入持久状态的入口也会被拒绝。

这意味着 Hook 可以为规划补充提醒或模型上下文，但不能借 Hook 运行命令绕过 Plan 边界。

终端 TUI 的新会话默认从 Plan 开始；恢复已有会话时保留其模式。空闲时可按 `Tab` 在 Plan 与 Build 之间切换；输入斜杠命令时，`Tab` 优先用于补全。手动切回 Build 只恢复原来的权限模式，不会批准或自动执行计划，计划文件仍保留。若原权限为只读，Build 仍为只读。

## 异步编排

在 PepsiCode 的 `settings.json` 中设置 `"orchestration": {"enabled": true}`，再打开会话即可启用实验性的 M1 编排。终端、桌面和 QQ 共用同一个内核入口。默认使用原有同步模式。

在 Build 中使用异步编排。协调者负责派工和汇总，Worker 通过现有审批与执行规则调查或修改项目。Agent 状态可看到排队和执行中的任务。桌面可点击“停止 Worker”，终端和 QQ 可发送 `/agent-stop <追踪编号>`。仍有其他 Worker 执行时，已进入等待回复的必答问题可以定向回答，协调者随后可申请新尝试。中断后发送文本“继续”进入恢复流程，未确认的操作需要先核对。

编排容量和节奏由 `settings.json` 的 `orchestration` 段控制，默认值如下；启动时会校验类型和范围，超出范围直接拒绝配置：

| 配置 | 默认值 | 作用 |
| --- | --- | --- |
| `maxConcurrentWorkers` | 3 | 同时运行的 Worker 上限 |
| `maxQueuedWorkItems` | 32 | 当前修订的排队上限；满时拒绝新派工 |
| `maxActiveWorkItemsPerRun` | 32 | 当前修订的活跃工作上限（含排队、执行、等待回复和待验收） |
| `maxAutoRetries` | 1 | 失败 `explore` 尝试的自动重试上限 |
| `maxCoordinatorActivations` | 20 | 每个修订自动唤醒协调者的次数上限 |
| `eventBatchLimit` | 8 | 一次协调输入的批内事件上限 |
| `eventContextChars` | 12000 | 一次协调输入的事件文本上限 |
| `eventDebounceMs` | 250 | 结果到达后的合并等待窗口 |
| `eventMaxDelayMs` | 1000 | 合并等待的绝对上限；用户输入和停止不被延后 |
| `coordinatorReserveRequests` | 2 | 从父任务预算中保留给协调收尾的请求数 |

`workerIsolation` 取代了早期草案中的 `workspaceMode`：`shared` 对应共享串行，`workspace` 对应独立隔离工作区（Git 仓库用 worktree，空目录或非 Git 目录自动使用独立目录工作区），`hardened` 仅在 Linux Bubblewrap 可用时生效且要求 Git 仓库基线。`workerTimeoutSeconds` 可配置单个受管 Worker 进程的墙钟超时（默认 300 秒）。无人看护授权仍由 `.pepsi-code/service/policy.json` 配置，不放进 `settings.json`。退出交互宿主会停止它持有的运行。

在空目录或非 Git 目录开启 `workspace` 隔离后，也可以直接并行派发实施 Worker（各自运行在 `.pepsi-code/m2/<work_id>/checkout` 独立目录中，互不干扰；`worker_reports` 保留各自工作区产出，`project` 支持无冲突合并回当前目录，仅远端推送/提 PR 仍需要 Git）。Agent 状态显示「排队」时，Worker 仍在等待宿主启动。

在空目录启动 PepsiCode，终端新会话显示 Plan 时，先在空输入框按 `Tab` 切到 Build。然后可以直接说「做一个双击就能用的离线单位换算网页，文件叫 index.html」。在共享或 `workspace` 模式下，主 Agent 可以直接在这个文件夹创建、运行和检查文件，无需预先初始化 Git 或填写 `verificationCommands`。完成后在原目录打开入口文件即可。

想看并行试做的结果时，可以说「让两个 Worker 分头试做，样例留在各自工作区，把改动和测试结果发我」。请在已有提交的仓库根目录启动 PepsiCode。从子目录启动时，派工提示会给出实际仓库根目录；在那个目录开启新会话即可沿用已有提交。成功派工后，输入 `/agents` 查看各个 Worker 的追踪编号与状态。

## 常驻服务（实验性，M3）

交互模式下运行归属当前交互宿主，退出会停止它持有的任务。需要“关闭客户端后任务继续”时使用常驻服务模式：

```sh
python -m pepsicode.service start --workspace E:/path/to/project   # 分离进程启动
python -m pepsicode.service status                                  # 查看身份与 PID
python -m pepsicode.service health                                  # 分项健康快照
python -m pepsicode.service attach                                  # 终端控制台 attach 到驻留会话
python -m pepsicode.service stop                                    # 显式停止（阻止守护器自动重启）
python -m pepsicode.service foreground                              # 供 NSSM/systemd 直接托管的前台入口
```

- 启动后连接信息写入 `<workspace>/.pepsi-code/service/`（`service.json`、`token`、`service.log`）。QQ 桥接在其配置中把 `backend.mode` 设为 `external` 并指向该地址与令牌，即可接入常驻服务。桌面端设置环境变量 `PEPSICODE_SERVICE_URL=ws://127.0.0.1:<端口>` 与 `PEPSICODE_SERVICE_TOKEN_FILE=<令牌文件路径>` 后直接接入服务（不再生成自有后端），并在界面上显示服务代次、待处理通知与“关闭窗口不会停止任务”提示。
- `service` 模式下关闭客户端只解除订阅，已授权任务继续推进；重新连接后通过会话快照和事件游标读取离线期间的消息、问题与结果。同一会话同一时刻只允许一个控制端连接。
- 停止任务（`turn/cancel`、`/stop`）、停止服务（`stop`）、断开客户端是三种不同动作。`stop` 会持久化人工停止标记，外部守护程序（NSSM/systemd/看门狗脚本）不能借普通 `start` 重启它；人工重新启动需 `start --clear-stop`。
- 服务模式要求先配置预授权策略（`<workspace>/.pepsi-code/service/policy.json`）：记录允许的路径、命令前缀、外部动作、费用/次数/时长上限与有效期。策略内的操作自动执行并留审计记录，策略外的操作等待接入的控制端确认；无人接入时拒绝并合并为通知，不会自动重试。撤销或到期立即在下一副作用入口生效。
- 可在 `settings.json` 配置 `"service": {"port": 0, "connectors": {"github": {"enabled": true, "repository": "owner/repo", "pollIntervalSeconds": 60, "tokenEnv": "GITHUB_TOKEN"}}}`，服务会只读轮询 PR 审查与 CI 状态，事件经去重后交给协调会话；连接器令牌只从环境变量读取。
- `python -m pepsicode.service health` 分项显示存储、队列、模型、Worker、连接器与通知健康。事件日志写失败会暂停新的任务处理，恢复后自动继续。
- 终端控制台 `python -m pepsicode.service attach [--session SESSION_ID|latest] [--list] [--url ...] [--token-file ...]`：连接后显示服务代次与待处理通知，`/sessions`、`/attach <id|latest>`、`/status`、`/notifications`、`/cancel` 查询与控制，普通文本在轮次空闲时开始新轮次、运行中自动转为 steer 排队输入；审批请求到达时输入 `y`/`n` 回答。退出控制台只 detach，运行中的任务继续推进（`/detach`/`/quit` 同义）。开始新轮次（`turn/run`）仍要求有效的预授权策略；排队输入与取消不受影响。停止服务仍只能用 `stop` 命令，控制台不提供 `service/shutdown`。

服务默认只绑定本机回环地址。跨主机部署、公网访问、服务账户与凭据轮换需要自行部署并审计，本版本未内置；服务与执行在同一台主机上，主机关机任务同样中断，重启后按策略恢复安全任务。

异步编排的 WebSocket 客户端在认证请求中声明 `orchestration: 1`。服务策略的请求次数、预估费用和运行时间与会话上限取较小值。GitHub 审查和 CI 事件先持久化，再投递到拥有对应 PR 的任务。旧 head 的结果不会覆盖已记录的新 head，事件作为内部任务数据投递。

## 交付闭环（实验性，M4）

启用编排的会话在 Worker 交付后可以走"候选 → 集成 → 验收 → 交付"的闭环，全部由宿主确定性代码执行、经执行层审计：

- 候选记录：Worker 尝试到达 `result_ready` 且其受管 worktree 有已提交改动后，可记录候选——绑定 base SHA、分支与提交后的 head SHA、有界的 diff 摘要。worktree 存在未提交改动时拒绝记录（交付必须是 commit）；head 或分支后续变化会把候选标记 `stale`，旧记录不能代替新代码。
- 集成与精确版本验收：协调者经 `task_integrate {work_ids}` 把多个候选按序 cherry-pick 到从共同 base SHA 建立的独立集成工作区（分支 `codex/integrate-<id>`），随后运行 `verificationCommands` 配置的验收命令，验收记录绑定集成 head SHA、任务修订与命令摘要。出现冲突时中止 pick 并保留双方改动（不强制丢弃、不 reset），集成记录进入 `conflict` 等待人工或协调者决策。候选或集成 checkout 在验收后发生变化会把整条记录标记 `stale`——旧验收证据不继承给新代码。未配置 `verificationCommands` 时不产生有效验收，交付被拒绝。
- 有限修复：失败的实现类尝试可按 `orchestration.maxRepairAttempts`（默认 2）重试；失败按原因分类（代码/环境可自动修复，权限/预算/传输/未知不进入自动修复），同一失败指纹的重试被拒绝（无进展不算修复）。Worker 最终明确报告 `failed`/`blocked` 时按同一尝试的实际结果结算并投递失败事件（不再伪装成待核验结果），事件附带有界证据：阶段、失败类别、失败检查、输出摘要与执行日志引用。重试尝试在核对 worktree、分支与新提交归属（线性接续已记录基线）后，接续同一工作项此前已确认的提交成果继续修复；被改写或来源不明的 HEAD 不自动接续，保留待核对。
- 修复基线与交付版本验收：集成验收失败后，同一任务修订内新派发的实施尝试默认从失败集成记录的 head 建立工作区（环境清单记录 `base_integration` 来源），保留原候选与失败证据；冲突中的集成无 head，不作为基线。重新集成通过后，父任务的验收条件和必需产物在**实际交付的集成 checkout** 上检查——绑定当前任务修订、干净 checkout 的 HEAD 与已确认的交付记录；交付版本发生变化（候选/checkout 变更、重新集成）或验收定义变化后，按新版本重新验收，旧交付记录不能直接通过。
- 交付：协调者经 `task_deliver {integration_id}` 交付**已通过验收**的集成。默认只生成本地交付记录（分支 + head + 证据引用）；`push: true` 推送到远端分支——交互模式经执行层审批，service 模式按预授权策略判定；推送前先 `ls-remote` 核对远端（已在远端则幂等跳过），推送结果未知时标记 `uncertain` 并合并通知，绝不自动重发。`pull_request: true` 经 GitHub API 为集成分支建 PR（令牌只从环境变量读取，`tokenEnv` 默认 `GITHUB_TOKEN`）；同一 head 已有 PR 时幂等返回，不重复创建。
- 保留：集成工作区不自动清理；`prune_integration` 只处理已通过验收、冲突或已过期的集成，且经执行层执行。

在 Git 项目中设置 `"orchestration": {"enabled": true, "workerIsolation": "workspace"}` 后，实施 Worker 从已经提交的 HEAD 自动准备独立 worktree。Linux 可显式选择 `hardened`，需要系统提供 Bubblewrap 和可用的用户命名空间。集成验收要求 checkout 在检查前后都干净。创建 PR 需要执行层批准，结果不明时后续调用只核对远端记录。

真实模型评测目前为小样本单 Agent 编码用例（1/3 通过，见修改清单第八批记录）。真实模型 M4 闭环、真实 GitHub 推送及建 PR、真实 CI 仍待实测。
