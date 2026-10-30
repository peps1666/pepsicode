# 代码理解、自动验证与工具发现

代码智能、自动验证和按需加载工具同时用于终端、管道 CLI 和 Electron 桌面端。以下配置合并到 `~/.pepsi-code/settings.json`，修改后重启会话。

## 1. 语义代码导航

`code_intelligence` 支持文件符号、定义跳转、引用、诊断，以及只读重命名预览。实际跨文件修改使用 `rename_symbol`，逐文件经过原有路径和编辑审批。输入行列从 **1** 开始，列按 Unicode 字符计算；原始 LSP 结果的位置从 **0** 开始，按 UTF-16 计算，并在响应中标明。

语言服务按需启动，会话结束时关闭。自动寻找 Python 的 basedpyright / pyright / pylsp、JavaScript/TypeScript 的 typescript-language-server、Go 的 gopls 和 Rust 的 rust-analyzer。语言服务是可选依赖，例如 Python 可安装：

```sh
python -m pip install -e ".[intelligence]"
```

没有可用服务时，符号、定义和引用退回语法或词法候选，明确标记为 fallback；诊断显示不可用，重命名不执行字符串替换。词法候选不能区分所有同名局部变量。首次启动语言服务也受命令审批约束。

可按语言覆盖启动命令、参数、超时（秒）、服务设置及子进程环境，或用 `false` 禁用某种语言：

```json
{
  "languageServers": {
    "python": {"command": "pylsp", "args": [], "timeout": 30},
    "typescript": {"command": "typescript-language-server", "args": ["--stdio"]},
    "rust": false
  }
}
```

`env` 是字符串到字符串的映射，仅影响该语言服务子进程。受限 Windows 环境下，Jedi 缓存目录应可写且路径较短；必要时可在 Python 服务配置中设置 `"env": {"LOCALAPPDATA": "E:/cache/lsp"}`。

语言列表可扩展，无需修改 Python 代码。例如接入已有的 C# 语言服务：

```json
{
  "languageServers": {
    "dotnet": {
      "command": "your-csharp-language-server",
      "extensions": [".cs", ".csx"],
      "languageId": "csharp",
      "initializationOptions": {"projectMode": "script"}
    }
  }
}
```

`command` 和初始化参数需按实际服务填写。内置语言也可仅覆盖 `settings`、`env` 或 `timeout`，继续使用自动发现的服务。重命名的标识符语法交给对应语言服务校验，不套用 Python 标识符规则。

## 2. 修改后的自动验证与修复

默认 `verificationMode: "auto"`：存在工具调用产生的待验证修改时，运行项目检查。Python 检查以实际测试文件或 pytest 配置为依据，只有名为 `tests` 的目录不会触发 pytest。递归寻找 `package.json` 中的 `typecheck`、`lint`、`test`、`build` 脚本；按最近的 `packageManager` 声明或锁文件选择 npm、pnpm、yarn、bun，无依据时使用 npm，声明冲突时要求显式选择。其他框架和工具链直接使用自定义 argv 命令。

每轮收集各项检查的结果，失败输出进入下一轮模型上下文，代理修复后重新检查。相同失败连续达到 `verificationNoProgressLimit`（默认 2）、达到总轮数或预算上限时停止；Ctrl+C / 桌面停止按钮会中止执行。代理调用澄清工具时立即交还给用户，保留待验证修改，获得回答后再继续。无测试、审批或 Hook 拒绝、未执行和失败均不会显示为通过。终端显示进度，桌面显示可展开的验证报告。

```json
{
  "verificationMode": "auto",
  "maxIterations": 3,
  "verificationTimeout": 120,
  "verificationCommands": [
    {"name": "Python tests", "command": ["python", "-m", "pytest", "-q"]},
    {"name": "Client types", "cwd": "pepsicode-client", "command": ["npm", "run", "typecheck"]}
  ]
}
```

`verificationCommands` 存在时替代自动发现，命令必须是 argv 数组，`cwd` 必须位于项目内；空数组表示没有配置检查。每项还可设置 `env`、`timeout` 和 `noTestsExitCodes`；后者用于声明自定义测试命令“未收集到测试”的退出码，默认不根据输出中的英文句子推断。Windows 包管理器参数支持安全的 argv，含 shell 控制字符时会明确报错，可改用直接可执行文件或包装脚本。每项命令默认超时 120 秒，可设 1–300 秒。`maxIterations` 默认 3，包含首次执行，显式设置的正整数不会再被静默截断为 10。可用 `budgetLimitUsd` 限制会话费用。

自动发现范围、检查脚本名和包管理器均可配置：

```json
{
  "verificationDiscovery": {
    "maxDepth": 6,
    "maxDirectories": 2000,
    "exclude": ["examples/legacy", "vendor"],
    "scripts": ["check:types", "lint", "test:ci", "build"],
    "packageManager": "pnpm"
  },
  "verificationNoProgressLimit": 3
}
```

默认深度为 6，目录数量上限为 2000；可按仓库规模调整。`exclude` 接受目录名或项目相对路径，默认跳过依赖、虚拟环境和构建产物目录。达到目录数量上限会明确报错，建议缩小扫描范围或配置命令。自动发现是项目约定的兜底，不替代产品验收标准；检查通过只说明这些检查覆盖的行为通过。

`always` 每轮都检查，`off` 关闭检查。旧 `verifierEnabled: true/false` 分别对应 always/off，显式 `verificationMode` 优先。只读和 Plan 模式不运行验证命令；其余模式沿用命令审批和工具 Hooks。

待验证状态和最近的验证报告随会话保存，终端及桌面恢复会话后都会恢复；暂停澄清后重启程序，也不会把草稿误判成“没有修改”。旧版本会话没有这类状态时，历史中存在可能修改项目的工具调用，会保守地重新检查一次。

## 3. 按需加载工具

默认 `toolLoading: "auto"` 根据完整工具定义的估算 token 数与上下文窗口决定：不超过 12,000 且不超过窗口的 15% 时预加载，否则按需加载。窗口缺少可靠配置时使用默认窗口估算，估算并非服务商实际 token 数。也可设置 `"eager"` 固定完整工具集或 `"lazy"` 按需发现。按需模式通过 `search_tools` 搜索，工具定义从下一次请求起提供；新增工具会使工具前缀变化，并写入执行日志。已加载列表随会话保存，旧会话则根据历史中已使用工具恢复。

可以直接要求代理“搜索数据库工具”“查找 Git 工具”或“查找 rename_symbol”。旧配置 `toolDiscoveryEnabled: false` 在自动模式下仍表示预加载；显式 `toolLoading` 优先。此机制不延迟 MCP 服务器连接，也不改变权限规则。工具注册或配置发生变化时，恢复后的工具视图可能不同。

需求尚未收敛时，`search_tools` 可用 `{"query":"*","limit":5,"offset":0}` 浏览全部能力，按 `offset` 翻页，不必命中预设关键词。工具名称、描述、参数名、`tags`、`aliases` 均参与搜索。`toolCoreTools` 可覆盖初始工具名单，`toolSearchAliases` 可扩展领域术语：

```json
{
  "toolCoreTools": ["read_file", "list_files", "grep_files", "run_command", "ask_user"],
  "toolSearchAliases": {"运力规划": ["fleet", "logistics", "route"]}
}
```

扩展工具可声明 `always_loaded` 及 `affects_workspace` 元数据；后者支持布尔值或按参数判断的函数。未知非只读工具保守触发验证，即使执行中先写入后抛错也会记录，不再通过固定工具名称判断是否修改项目。只更新交互、任务列表或记忆的内置工具显式退出这类验证。

可选 `promptCaching: true` 会在 Anthropic 兼容请求中启用自动缓存参数，默认关闭；仅在接口明确支持该参数时开启。多厂商配置可在对应 `providers.<id>` 内单独设置。缓存是否命中、费用和可用性以实际接口返回为准。

## 4. 对话分支

`/tree` 查看消息树，`/tree <id>` 切换路线；用户消息选中后返回到该消息之前，原文作为草稿。`/fork <用户消息id>` 新建独立会话，`/clone` 复制当前路线，`/retry` 在新分支重新执行最后一条用户消息。桌面端提供对话分支面板与消息按钮。

所有操作共用工作目录，不恢复文件快照。重新生成可能再次执行工具，使用当前模型和权限并继续自动验证。原始消息独立于压缩上下文保存；切换路线清空旧摘要，不回退文件验证状态。旧会话按现存记录升级。

## 5. 上下文 checkpoint 可靠性

语义压缩按估算 token 完整分块读取待压缩转录，包括长消息和工具输出的尾部；不再先截取每条消息或转录的前 15,000 字符。旧 checkpoint 与新增记录共同进入摘要，超出预算时继续归并，不直接裁掉摘要中间的内容。摘要要求保留目标、用户约束、决策与失败方案、已完成工作、未解决问题、下一步及恢复引用。

摘要为空、请求失败、输出被截断、归并无进展或超过调用预算时，本次压缩不提交，原上下文保留，并在上下文状态中显示跳过原因。取消信号继续向上传播。只有没有配置语义摘要器时，才使用显式标为降级的抽取式 checkpoint。每个成功的压缩边界记录摘要方式、调用次数和分块数；上下文状态以临时文件加原子替换保存，写入失败不会截断已有 checkpoint。

分块预算使用项目的中英文 token 估算器，不是厂商精确 tokenizer。完整读取和保守失败处理能避免确定性的截断丢失，语义摘要仍可能遗漏信息；用[真实任务评测](task-evaluations.md)中的 `compaction` 和 `resume` 场景测量这种风险。

## 6. Harness 执行边界

文件、进程与外部修改已接入共同的执行入口；任务链共享请求预算、取消和未知结果状态。文件编辑使用版本检查和原子替换，会话持久化执行日志指针，恢复后先核对未知结果再继续。默认进程执行仍为应用层审批；原生 OS 隔离后端尚未实现，选择限制模式时拒绝无隔离执行。具体保证、配置、恢复流程与测试范围见 [harness-boundaries.md](harness-boundaries.md)。

## 7. 用户任务生命周期

用户任务与执行轮次分别记录。实现和操作任务需要声明具体验收条件；一般回归全绿不能直接把任务标为完成。需求变更会增加任务版本并清空旧证据，验收条件的替换须引用真实用户输入。运行中输入先持久化再确认接受，Todo 与澄清问题归属各自会话；截断模型输出不会派发工具或声明成功。恢复不会自动唤醒模型。接口、状态、修订和能力边界见[任务编排](task-orchestration.md)。
