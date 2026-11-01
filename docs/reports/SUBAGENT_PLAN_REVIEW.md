# 子 Agent 改造计划评审：修改建议与原因

> 评审对象：子 Agent 四阶段改造计划（结构化结果 → 能力与委派契约 → 受控只读并发 → 按需扩展）。
> 评审依据：当前工作区代码阅读（2026-11-01），未运行真实模型，未修改代码。
> 用途：给计划作者修订计划用。每条给出「问题 / 证据 / 原因 / 建议 / 验收」。

## 结论先行

1. 方向正确，优先级（结构化结果 + 明确契约 + 受控只读并发）也选得准。
2. 但**阶段三的“启动、查询、等待、取消”是过度设计**。现有工具批处理已经能提供只读并发；引入后台句柄会与“父任务完成前必须结算”、验收和恢复机制冲突。建议改为“批量只读并发 + 同步 join”，后台生命周期推迟到有测量证据之后。
3. **阶段二的大部分底座已经存在**（共享预算、取消、未知结果、journal），不要重造；真正缺的是“子任务能读到父任务契约并在每步校验 revision”。
4. 阶段一不只是改标题：当前取消会被报成成功、max_steps 会报成 failed 但标题写 completed、unknown 会重复登记。这些是必须先修的缺陷。

## 阶段一：结构化结果

### S1-1（阻断）状态必须从单一来源派生

**问题**：`task` 工具根据最后一条消息的 `isError` 推断状态（`pepsicode/tools/task.py:226-246`），而结束原因在 `run_agent_turn` 内部被丢失或改写。

**证据**：

- 取消：`TurnCancelledError` 被转成普通助手消息 `"Task stopped by user."`（`pepsicode/core/agent_loop.py:392-397`、`1361-1364`），没有 `isError`。`task` 工具因此返回 `ok=True`、`status="completed"`，标题写“completed”。
- 达到轮数上限：`run_agent_turn` 追加带 `isError` 的“Reached the maximum tool step limit”（`agent_loop.py:1641-1652`），`task` 工具判为 failed，但标题仍是硬编码的 `"[Sub-agent ... completed ...]"`（`task.py:237`）。
- `awaiting_user` 分支对子代理不可达：`ask_user` 在 `GLOBAL_DISALLOWED` 中（`pepsicode/agents/tool_filter.py:30-35`），子代理工具池也没有它。

**原因**：状态、标题、trace 三处各自推断，必然不一致；模型看到的结果文本还会把“取消”当成“完成”。

**建议**：让 `run_agent_turn` 返回结构化结束原因（`completed / cancelled / timed_out / max_steps / blocked / failed / unknown`），`task` 工具只做映射；标题由状态生成，不再手写。复用现有 `ToolStatus`（`pepsicode/tooling.py:113-123`），不要新造词表。

**验收**：正常、取消、超时、达到 max_steps 四个用例的 `ToolResult.status`、标题和 trace 状态一致；取消不得为 `ok=True`。

### S1-2（阻断）子任务的 unknown 不要用父 call_id 再登记一次

**问题**：若 `task` 工具返回 `status="unknown"`，`ToolRegistry.execute` 会用父调用的 `tool_use_id` 再 `mark_unknown`（`pepsicode/tooling.py:495-498`、`515-516`），而子任务自己的工具调用已经登记过未决调用。

**原因**：产生两个指向同一副作用的未决调用，父级 reconcile 时对不上；也可能把一个“只读子任务内部报错”升级成父级写阻塞。

**建议**：子级 unknown 只登记子 call_id；`task` 工具返回 `blocked` 或 `failed` 并在文本中引用子 call_id 和工具名，不自我标 unknown。共享的 `ExecutionScope.unresolved` 已经会让父级停止写操作（`pepsicode/execution/runtime.py:260-276`），不需要重复登记。

**验收**：子任务出现未知结果后，父任务只有一组可核对的 call_id；父级 `execution_status` 能定位到具体子调用。

### S1-3（阻断）异常路径不能把“没执行”变成“未知副作用”

**问题**：`_run` 只把 `run_agent_turn` 包在 try 里（`task.py:203-224`），模型适配器构造、trace 创建、预算检查等异常会冒到 `ToolRegistry._execute`，命中“已派发且非只读 → `unknown`”（`pepsicode/tooling.py:680-687`）。

**原因**：一次根本没有启动的子任务会阻塞父级后续所有写操作，并留下无法核对的操作记录。

**建议**：`_run` 全路径捕获并转成结构化失败；只有确实发生了副作用（`effect_state` 已提交）才标 unknown。

**验收**：模型未配置、trace 异常、预算耗尽三种情况下父级不被未知结果阻塞。

### S1-4（重要）验证报告只作输入，不要接进验收

**问题**：计划已明确“子 Agent 验证报告只作为证据输入，仍由现有验收机制判断”，方向正确，但缺少机制保证。

**原因**：一旦结构化结果被 `task_state`/acceptance 读取，就等于新开一条绕过条件检查的通道；模型的 `VERDICT: PASS` 不是证据。

**建议**：结构化结果只进入对话上下文和 UI；在结果中标注“claims, not evidence”；父级要证据时用 `task_state` 的 `command` 条件重跑。加一条回归测试：子代理声称 PASS，验收不能通过。

**验收**：伪造“子代理说测试通过”不会让任何验收条件变绿。

### S1-5（重要）“实际改动”要用执行证据，不用模型自述

**问题**：计划要求结果包含“实际改动、检查结果”，但子代理只能自述。

**建议**：从子任务自己的工具调用/执行日志推导（按 child scope 的 task_id 过滤 `tool/started` 中 `mutating=true` 的记录），并标注“推断，未验证”；模型自述单独标为 claim。注意 `task` 工具输出最终会被 `max_result_size_chars` 截断（默认 4000），结构化摘要要短，细节放正文。

**验收**：结果中的“改动文件”与 journal 记录一致；无法推导时明确写“未知”，不猜。

## 阶段二：能力与委派契约

### S2-1（阻断）子任务现在看不到父任务契约

**问题**：`ExecutionScope.child()` 不复制 `control`（`pepsicode/execution/runtime.py:260-276`），而 `task_control.owner()` 对带 `parent_task_id` 的 scope 直接返回 `scope.control`（空字典），不返回 session（`pepsicode/core/task_control.py:18-31`）。子任务内部的工具调用 `snapshot()` 得到空任务。

**后果**：

- `ToolRegistry._execute` 的 `read_only_request` 检查（`tooling.py:546-553`）对子任务不生效。用户在子代理运行中把需求收紧为“只分析”，已在跑的一般子代理仍可继续写文件；一个 general 子代理可跑 15 轮，窗口不小。
- 子任务无法读取父任务 `kind` 和 `revision`，无法在修订变化时停止。
- 计划中“固定父任务 ID、修订号”目前没有落点。

**建议**：给子任务一个**只读的任务投影**（task id、revision、kind、约束、验收条件摘要），每个 step 和每个 mutating 调用前校验 revision；子任务不得通过 `edit_control` 修改父任务（那会 bump 父 revision，属于扩大权限）。

**验收**：子任务运行中收到“停止修改，只分析”后，其下一个写工具返回 blocked；revision 变化后子任务显式返回 stale，而不是继续用旧权限。

### S2-2（重要）先对齐三套身份，不要新增第四套

现状已有：用户任务 `task_control` 的 id + revision、`ExecutionScope.task_id`（预算/取消/journal）、`trace_id`。契约里应同时携带“用户任务 id + revision”（决定证据是否有效）和“执行 scope id”（决定日志/取消归属），不要混用或再造一套。

### S2-3（重要）能力必须由最终工具集反推，并做加载校验

**问题**：

- `isReadOnly` 用 `bool()` 解析，字符串 `"false"` 会变成 True；`maxTurns` 用 `int()`，非法值直接抛异常（`pepsicode/agents/loader.py:162-189`）。
- `allowedTools` 中不存在的工具名被静默丢弃；不写 `allowedTools` 的自定义 agent 默认拿到全部写/执行工具（`tool_filter.py:72-93`、`_SUB_AGENT_TOOL_POOL`）。
- `verification` agent 声明 `isReadOnly=False`，但持有 `run_command`，它不是只读的（`pepsicode/core/sub_agents.py:101-123`）。
- Plan/只读模式按名称硬编码 `{"explore", "plan"}` 判定（`pepsicode/permissions.py:286-315`），自定义只读 agent 或改名会失效。

**建议**：加载时校验（未知字段、未知工具、类型、范围、重名）；能力从解析后的工具集反推（含 `run_command` 即“可执行/可写”）；在工具结果和 UI 显示“实际获得的工具 + 定义来源路径”；Plan 模式改为能力判定。

**验收**：写错工具名/`isReadOnly: "false"`/`maxTurns: "abc"` 都有明确报错；自定义只读 agent 在 Plan 模式下可用；结果中能看到有效工具列表。

### S2-4（阻断/需决策）项目级 agent 可以覆盖 builtin，且热加载

**问题**：`AgentLoader` 按“项目 → 用户 → builtin”解析同名文件（`pepsicode/agents/loader.py:113-128`），项目 `.pepsi-code/agents/*.md` 可以替换 `explore`/`general`/`verification` 的系统提示，且每次调用按 mtime 热加载。

**原因**：工具集没有扩大，但“指令权限”扩大了——被打开的仓库可以写一个 `general.md`，引导子代理用 `run_command` 外传源码；用户看到的仍是 “General”。

**建议**：三选一并写进计划：(a) 项目定义禁止覆盖 builtin 名；(b) 允许但需显式信任/确认；(c) 至少在使用时显示定义来源路径并在结果中标注。推荐 (a) 或 (b)。

**验收**：仓库内新增/修改 agent 文件不能静默改变 builtin agent 的行为。

### S2-5（重要）预算共享已有，但没有子任务配额

`RequestBudget` 是共享的（`runtime.py:260-276`），子任务不能重置预算，这已经满足“共享预算”。但一个 general 子代理可以花光全部额度，`maxTurns` 不是成本上限。建议加 per-child 的请求数/费用上限，从剩余额度派生，子任务不能放大。

### S2-6（重要）取消语义要区分“干净取消”和“可能有副作用”

取消信号已经能传播（`CombinedCancellation`），但取消不等于回滚。建议在契约里区分：只读子任务的取消可安全重试；写/执行子任务取消后若存在已派发调用，必须是 `unknown` 并进入核对流程，不能直接重跑。

### S2-7（重要）审批请求缺少子任务归属

`LocalApprovalBackend` 没有锁，`ApprovalRequest.to_payload()` 也没有子任务标识（`pepsicode/approval/local_backend.py`、`pepsicode/approval/types.py`）。一旦并发子任务能触发审批，会出现多个提示同时弹出、无法区分归属。至少先加 agent/子任务 id 到 payload，并串行化提示。

## 阶段三：受控只读并发

### S3-1（阻断，设计层面）不要先做后台句柄

**问题**：计划的“启动、查询、等待、取消”意味着子任务在父 turn 结束后仍可能运行，直接冲击：

- “父任务完成前必须结算”：任务状态、验收、`turn/end` 都要新增 pending 集合，还要跨 turn 持久化和恢复。
- 验收可能在工作区仍被修改时通过。
- 会话恢复后要重新接管子任务，预算/取消/所有权都要重新建模。

**建议**：先做“批量只读并发 + 同步 join”：同一条 assistant 消息里发多个 `task` 调用，用现有 `_execute_calls_in_order` 的线程池并发执行，全部结束后按调用顺序返回结果。这已经提供并发和等待，且天然保证父 turn 结束前所有子任务已结算。后台句柄只作为后续选项，前置条件是持久化、恢复、pending 结算、进度事件。

**验收**：一次消息内 3 个只读 task 并发执行，结果顺序与调用顺序一致；父 turn 结束时不存在运行中的子任务。

### S3-2（阻断，前置 bug）线程池不继承 ContextVar，任务策略会静默失效

**问题**：`current_session()` 基于 `ContextVar`（`pepsicode/core/session_tree.py:15-19`），`ThreadPoolExecutor` 不复制上下文（`agent_loop.py:542`）。在 worker 线程里 `snapshot()` 会退化为空任务（`tooling.py:501`、`548`）。

**后果**：一旦 `task` 被标记为可并发，它在 worker 里执行时读不到 session/task：`read_only_request` 检查、重复检测、验收相关状态都会静默为空。这是并发改造必须先修的。

**建议**：显式把 session/task control 绑定到 worker（或在主线程完成策略检查后再派发）；加一个并发下的只读约束回归测试。

**验收**：并发 `task` 调用中，用户已收紧为只读时，写子任务仍被拒绝。

### S3-3（重要）并发判定要按参数，不能只看工具名

`ToolDefinition.concurrency_safe` 是静态 bool，`_is_concurrency_safe` 只看工具名（`pepsicode/tooling.py:176-179`、`agent_loop.py:120-122`、`534-546`）。但 `task` 是否只读取决于 `agent_type`。需要 per-args predicate，否则 general/verification 也会被并发。

### S3-4（阻断）“只读”的定义要写死

建议定义为：**无写文件工具 + 无进程执行 + 无外部修改**。`run_command` 一律算写：测试、构建、lint 都会写缓存和产物（`.pytest_cache`、`dist`、`node_modules`），还会触发审批。按此定义，`verification` 子代理不属于只读并发集合。

**验收**：只读并发集合内的子任务不可能弹审批、不可能产生文件变更（可用 journal 的 `mutating` 记录断言）。

### S3-5（重要）审批、UI、Hook 的线程安全未证明

`_execute_calls_in_order` 的回调目前在主线程调用（先 `start` 再提交、结束后 `finish`）。接子任务进度时不要把回调放到 worker 线程；审批提示必须串行并带归属。只读批处理本身不弹审批，但要保证这一点不被后续改动破坏。

### S3-6（后续）有金额上限时请求仍串行

`RequestBudget.request` 在有金额上限时持锁到传输结束（`pepsicode/execution/runtime.py:149-169`），并发收益会被吃掉。这是已知的 ISSUE-011；先测量，再决定是否做原子预留，不要为并发削弱预算核算。

### S3-7（重要）修订关联要进结果

结果携带 `spawn_revision` 和 `return_revision`；不一致时标 stale，并写明“这是 revision N 的观察，当前 revision M”。验收机制会清旧证据，但模型会把旧结论当现状，需要显式提示。

### S3-8（后续）写入并发继续谨慎

同意计划。补充两点：多文件修改不是事务，文件版本检查只保护单文件且只协调遵守锁的写入者；子任务写完文件后，父级的读取版本可能过期，下一次编辑会报 stale，应给出“请重新读取”的明确提示，而不是笼统的版本错误。

## 阶段四：按需扩展

- **嵌套子 Agent 维持最后**。现有 `GLOBAL_DISALLOWED` 已经禁止递归（`tool_filter.py:30-35`），不要为自定义 agent 打开；嵌套会同时放大预算、取消、所有权和审计复杂度。
- **长期运行/后续消息/写入隔离**的前置条件不是“并发已经跑通”，而是：持久化子任务状态、恢复后重新接管、pending 结算、进度事件、审批归属、每子任务预算配额。缺一项都不应该对外承诺。

## 建议的阶段重排

| 里程碑 | 内容 | 完成条件 |
| --- | --- | --- |
| M0 | 阶段一 P0：状态单源、取消/超时/max_steps、unknown 归属、异常路径 | 四类结束原因状态一致；无重复未决调用；父级不被“未执行”阻塞 |
| M1 | 阶段二 P0/P1：任务只读投影、revision 校验、agent 校验与来源显示、审批归属 | 中途收紧权限能阻止子任务写；非法 agent 配置报错；结果可见有效工具 |
| M2 | 阶段三 P0/P1：线程上下文修复、per-args 并发、严格只读批量并发 + 同步 join | 并发只读不弹审批、不写文件；结果顺序稳定；turn 结束无运行子任务 |
| M3 | 测量后：后台句柄/长期运行/写入隔离/嵌套 | 持久化、恢复、结算、配额全部就绪后再逐项开放 |

## 不要重复实现

- 共享预算、取消信号、未知结果集合、子任务 journal：`ExecutionScope.child()` 已经共享，直接复用。
- 工具过滤分层（global disallow / 只读边界 / disallow / whitelist）：`resolve_agent_tools` 已经实现，扩展它而不是重写。
- 验收与子任务结果分离：现有 `task_state` 机制是对的，保持并测试，不要接新通道。

## 需要拍板的决策

1. 结构化结果主要给模型看，还是给宿主/UI 看？（建议两套呈现，模型侧用短文本，宿主侧用数据类）
2. 子任务结果是否算验收证据？（建议不算，只影响模型推理）
3. 项目级 agent 是否允许覆盖 builtin 名称？（建议禁止，或显式信任）
4. “只读”是否包括跑测试/构建？（建议不包括，`run_command` 一律算写）
5. 后台子任务是否真的需要？（建议先做批处理，用真实使用数据决定）
