# 用户任务处理逻辑：对照 dsh 的审查与迁移建议

日期：2026-10-26。

> 本文记录改造前的审查与探针结果。对应实现、当前保证及限制见[用户任务生命周期与验收](task-orchestration.md)。下文“当前”均指审查时的版本。

审查对象是本机 `E:/Code/pepsicode` 的当前工作区（包括上一轮尚未提交的执行边界改动），以及 `E:/Code/LLM/deepseek-harness`，dsh HEAD 为 `22ddee3792402c3e70ec5f86a63c3e3e895a2f60`。这是本地源码比较，不代表对 dsh 上游最新版本的判断。

## 结论

上一轮改动强化的是执行安全与可恢复性。它没有建立完整的用户任务领域模型。现在最需要学习 dsh 的地方是：**把用户输入、执行轮次、目标状态和续行权限分别建模，再定义它们之间的转换。**

当前 pepsicode 更接近：消息 → agent loop → 工具 → 助手最终文本 → 项目检查 → 返回消息。dsh 则有持久 inbox、明确的 turn/step 边界、停止前扩展点，以及独立于物理轮次的 goal 和续行驱动器。

这里有一个重要限制：dsh 的普通 turn 也会在没有工具调用、没有待交付输入时结束；它的 goal complete 工具主要检查身份、权限和版本，仍依赖模型判断目标是否完成。**不能把 dsh 描述成自动证明所有用户需求都已满足的系统。** 我们应迁入它的生命周期机制，并把现有验证器接到具体任务的验收条件上。

## 已执行的离线探针

探针使用故意早停或忽略错误的脚本模型；流式截断场景使用真实 Anthropic 适配器解析模拟 SSE。没有调用真实模型 API，唯一验证进程执行 `assert True`。因此这些结果证明的是 Harness 当前允许哪些轨迹，不是实际模型失败率。

用户任务明确要求实现 `required_feature.py`。结果如下：

| 场景 | 实测结果 | 暴露的逻辑边界 |
| --- | --- | --- |
| 模型只回答“接下来实现” | 没有文件；verification=`not_run/no_changes`；execution=`succeeded` | 正常结束与用户目标完成缺少独立判定 |
| 配置一个无关的绿色检查 | 没有文件；verification=`passed`；execution=`succeeded` | 项目检查通过不能代表本次需求完成 |
| 读文件失败后，模型直接宣称完成 | 失败反馈已注入，但仍以非错误最终回复结束 | 恢复提示是软指引，没有任务完成约束 |
| 两个独立用户任务通过 follow-up 连续执行 | 两次模型调用只有一个 ExecutionScope task ID | 执行批次身份与用户任务身份混用 |
| 同一 Todo 更新，然后另一个会话写 Todo | 更新后 ID 改变；模块全局列表最后只含另一个会话的任务 | Todo 没有会话隔离，也没有稳定的更新身份 |
| SSE 明确返回 `max_tokens`，文本只有 `I will start by` | 半句话成为非错误最终回复，execution=`succeeded` | 流式结束原因在适配层丢失，结束标记被当作正常完成 |

本地复现脚本：`.eval-work/task_semantics_probe.py`；输出：`.eval-work/task-semantics-probe.json`。临时工作区运行后会移除，以上结果保留在本文。探针是本次审查工具，未修改生产任务处理行为。

`execution=succeeded` 本身可以合理表示“执行正常退出”；问题是它不能充当任务验收状态。此外 `loop_engine.py` 当前确实会在 verification=`not_run` 时调用 `task.complete()`，这已把不同层次的状态混在一起。

## 1. 先分清目标、任务、轮次与步骤

dsh 的 `ReactLoopAgent.turn()` 管理物理交互，`step()` 管理模型与工具；`goal` 服务管理跨轮次目标。`turn/end` 的 `completed` 不等同于 goal 的 `complete`。

对应源码：

- [dsh turn/step 驱动](E:/Code/LLM/deepseek-harness/packages/core/agent-loop/src/agent.ts:320)
- [dsh GoalPhase / GoalActivation](E:/Code/LLM/deepseek-harness/packages/goal/goal/src/types.ts:47)
- [pepsicode 通用 loop task 与完成判定](E:/Code/pepsicode/pepsicode/core/loop_engine.py:205)
- [pepsicode 以最后消息推导执行状态](E:/Code/pepsicode/pepsicode/execution/runtime.py:357)

建议分别保留：

- `ExecutionRun`：预算、取消、进程和审计；可覆盖一次前台运行，不再假定它就是一个用户任务。
- `UserTask`：原始请求、当前目标、revision、任务类型、状态、验收项、未解决事项。
- `Turn` / `Step`：传输与执行边界，携带结构化结束原因。
- 可选长期目标：只在用户授权持续工作时启用自主续行；普通问答不应自动升级成长时间执行。

任务类型至少区分问答、分析/审查、实现和操作请求。不能为了阻止“只说计划就停”，反过来要求每个问答都必须修改文件或执行测试。模型提议的验收项也不能悄悄变成用户新增需求。

## 2. 用户输入先成为可恢复事实，再被调度

dsh 的 followup、steer、inject 共享 inbox，但分别表达下一轮、下一步和不主动唤醒的上下文注入。插入、修改、领取和取消均写 `agent/inbox/spliced`，带稳定 message ID。

pepsicode 已有 `steer` / `follow_up`、容量上限、原子出队和退回待处理文本；这些机制应保留。但 `TurnInputQueue` 仍是内存状态，服务端 enqueue 返回确认后没有同步持久化该输入，SessionData 也没有待处理 inbox。当前测试主要覆盖进程存活期间的竞争，而不是接受消息后的崩溃恢复。

对应源码：

- [dsh durable inbox](E:/Code/LLM/deepseek-harness/packages/core/agent-loop/src/inbox.ts:186)
- [dsh 三种消息入口](E:/Code/LLM/deepseek-harness/packages/core/agent-loop/src/agent.ts:172)
- [pepsicode 内存队列](E:/Code/pepsicode/pepsicode/core/input_queue.py:62)
- [pepsicode enqueue 确认路径](E:/Code/pepsicode/pepsicode/server/app.py:788)

迁移时应建立 `accepted → claimed → admitted → settled/cancelled/returned` 语义，并在 UI 确认接受前达到规定的持久化边界。崩溃恢复可以做到不丢已接受输入，但不能承诺模型请求或外部副作用绝对 exactly-once；结果不明仍沿用上一轮的核对机制。

还应记录宿主赋予的输入来源：human、steering、policy、tool-feedback、subagent、goal-round。它决定控制权限，不能只靠一段 `role=user` 的文本判断。dsh 的 goal authority 就区分顶层人类消息与自动轮次，子代理的汇报不会自动获得修改父目标的权限。

## 3. 提供明确的停止决策点

dsh 在结束 turn 前执行 `agent/turn-stopping`，然后重新检查是否有新的 next-step 输入。完成、续行和取消不是散落在每个工具中的特例。

pepsicode 的同步与流式主循环分别维护一套结束分支，普通文本或 `<final>` 能触发返回。外层 ProjectVerifier 再运行项目检查，但没有统一的任务完成决策。

建议引入不依赖传输方式的 `TurnDecision`：

```text
continue_step       工具结果、新 steering 或具体剩余工作需要继续
wait_user           必要问题未回答，保留目标和现场
finish_turn         本轮交互结束，但不自动声明任务已完成
complete_task       验收条件已被当前证据覆盖
blocked             存在具体外部阻碍，并记录需要什么才能继续
cancelled           用户或所有者停止执行
budget_exhausted    到达执行上限，保留未完成状态
unknown             副作用结果不明，先核对
```

完成决策应读取 task revision、未回答问题、未结算工具、仍在推进的子任务和验收证据；不要根据最后一句话的语气判断。普通答疑的证据可以是分析与来源，并不必然是测试。

## 4. 不把项目测试当作需求验收

当前 ProjectVerifier 的项目发现、测试、lint、build 是有价值的回归检查，应继续使用。缺口是缺少“这些检查对应哪条用户需求”的映射。当前验证待办主要由 mutation counter 驱动；用户改变要求而没有新文件改动时，新验收义务也可能被忽略。

建议为 UserTask 建立可审查的验收项，以及 `satisfied / unsatisfied / unknown / not_applicable` 结果，每项关联：

- 当前任务 revision。
- 产物路径或外部结果。
- 验证方法、执行结果与限制。
- 证据适用的工作区版本，避免沿用修改前或旧要求下的绿色结果。

`tests_passed`、`artifact_exists`、`task_complete` 必须分别表示。模型评价可作补充，不能替代可检查的文件、行为或用户确认；无法机械验收的部分应保留明确的不确定性。

dsh 现有 `goal.complete()` 也没有通用需求证明器。它提供的是目标领域、CAS 更新和权限边界，我们应在此基础上结合自身的验证器，而不是复制一个 `complete` 工具就认为问题解决。

## 5. 自动续行要有版本和权限

dsh goal 更新使用 goal ID + revision。`goal-round-driver` 区分 queued、claimed、admitted，在异步准备前后重新检查目标是否仍有效；续行前等待持久化 checkpoint 和子任务，并让竞争的人类输入先执行。

更值得学习的是 `phase=active` 与 `activation=armed` 分离：持久记录一个未完成目标，不等于重启后仍被授权自动运行。新生命周期不会无条件继承旧驱动器的自动权限。

对应源码：

- [dsh 续行的 checkpoint 与子任务等待](E:/Code/LLM/deepseek-harness/packages/goal/goal-round-driver/src/index.ts:146)
- [dsh 对过期续行的执行前校验](E:/Code/LLM/deepseek-harness/packages/goal/goal-round-driver/src/index.ts:365)
- [dsh goal 工具权限](E:/Code/LLM/deepseek-harness/packages/goal/tool-goal/src/authority.ts:79)

pepsicode 迁移时应做到：steering 修改同一任务的 revision；follow-up 创建独立任务；旧 revision 的排队续行不可继续执行。等待期间新来的用户指令要重新仲裁，暂停与取消不能被模型自己的“继续”提示解除。

这里不承诺中途 steering 能撤销已经启动的工具。dsh 也在步骤边界交付普通 steering；立即停止需要明确的取消路径。

## 6. 结束原因必须穿过模型适配器

dsh 的模型完成结果有结构化的 max-tokens、error、aborted 等原因，turn 对截断采用保留语义；截断步骤不会照常分发模型工具调用。

pepsicode `StepDiagnostics` 在非流式路径带 stopReason，但恢复逻辑主要处理空的 thinking 截断。流式 `StreamToken` 则没有结束原因字段，SSE `message_stop` 只产生 `done`，`message_delta.delta.stop_reason` 未作为控制信息保留。

这是应优先修复的真实缺口：统一 `ModelStepResult`，让同步/流式只负责不同形式的输入输出；最终进入同一状态机。截断不能被解释成任务完成，截断响应中的工具调用也不能未经完整性策略判定就分发。

对应源码：

- [dsh max-tokens 的结束判断](E:/Code/LLM/deepseek-harness/packages/core/agent-loop/src/agent.ts:577)
- [pepsicode StreamToken](E:/Code/pepsicode/pepsicode/types.py:67)
- [pepsicode SSE 完成标记](E:/Code/pepsicode/pepsicode/llm/anthropic_adapter.py:297)
- [pepsicode 流式最终回复路径](E:/Code/pepsicode/pepsicode/core/agent_loop.py:1414)

## 7. 问题、Todo 和停滞检测属于任务控制面

### 用户问题

pepsicode `ask_user` 已能等待用户并阻止外层验证继续；但问题缺少独立 request ID、answer 关联和持久状态。dsh 的 user-questions 服务区分真正回答、用户跳过和超时仍 pending；timed 模式允许继续独立工作，后来回答带 callId 回到任务。

迁移重点应是必要问题形成依赖，不相关工作可以继续；超时不能被当成同意，用户的普通补充不能误当成某个审批的答案。

### Todo

pepsicode `todo_write.py` 的 `_tasks` 和计数器是模块全局，清空列表后再查旧条目，导致更新身份不稳定，多会话共享同一存储。dsh 按调用 agent 的 Session 写 `todo/write`，可重放并由 UI 投影。

Todo 应是会话中的进度事实，而不是跨会话的全局展示变量，也不能靠“模型把勾都打上了”证明任务完成。

### 停滞

dsh repeat-tool-reminder 对规范化工具名和完整参数计数，默认在 3/5/8 次提醒，权限拒绝也计入；真正的人类输入会重置链。它是提醒策略，不是自动证明没有进展的算法。

pepsicode 已有验证失败 fingerprint 和通用工具恢复提醒，但在同一轮内重复相同调用时，主要依赖 step / request 上限兜底。可以迁入调用链检测，再结合结果是否变化、任务 revision、产物是否推进判断停滞。预算耗尽、明确权限拒绝和等待用户的处理应独立于“重复三次”的经验阈值。

对应源码：

- [dsh timed question 的 pending 语义](E:/Code/LLM/deepseek-harness/packages/interaction/tool-ask-user/src/timed.ts:39)
- [dsh 会话 Todo 写入](E:/Code/LLM/deepseek-harness/packages/todo/tool-todo/src/index.ts:193)
- [dsh 重复工具检测](E:/Code/LLM/deepseek-harness/packages/guard/repeat-tool-reminder/src/index.ts:196)
- [pepsicode 全局 Todo](E:/Code/pepsicode/pepsicode/tools/todo_write.py:8)

## 建议的实施顺序

| 顺序 | 交付内容 | 验收轨迹 |
| --- | --- | --- |
| P0-A | 统一模型结束原因和 StepResult | max_tokens、取消、流式中断不再显示为正常完成；截断步骤不误执行工具 |
| P0-B | 区分 execution / turn / task 状态，建立完成决策入口 | 实现类任务只输出计划不能 complete；not_run 不直接 task.complete；普通问答仍可正常结束 |
| P0-C | 持久化 inbox 与会话 Todo | 接受输入后崩溃仍可恢复；领取不重复；两个会话不串 Todo |
| P1-A | Task revision、来源与验收证据 | 用户改变需求后旧验收失效；子任务不能擅自修改父目标 |
| P1-B | 有权限和版本约束的自主续行 | 人类输入优先、旧续行失效、暂停不可自解、重启不擅自重新启动 |
| P1-C | 结构化问题与停滞策略 | 必答问题阻止依赖步骤；超时不作答；重复调用反馈驱动策略变化 |

这些能力应进入共享任务运行时，CLI、TUI、桌面端只提供输入和展示。保留当前检查过的 FILES / Process / External 入口、共享预算、未知结果核对和 ProjectVerifier，不为学习 dsh 而把 Python 项目重写成 Cordis 插件树。

## 回归评测应增加的任务

1. 要求修复缺陷，模型只输出计划后尝试结束。
2. 现有测试全绿，但用户要求的新功能缺失。
3. 验证时用户提出新约束，旧结果不能验收新 revision。
4. 已接受的 follow-up 在进程退出后恢复，不丢失也不重复交付。
5. 执行中暂停，排队的自动续行不得唤醒旧任务。
6. 子任务未结算或返回未知结果，父任务不得宣称全部完成。
7. 工具连续报同样错误，必须调整策略、等待用户或明确未完成。
8. 语义压缩 / 会话恢复后仍保留原目标、约束、未答问题和未满足验收项。

本轮只完成源码审查与离线复现，没有改动生产任务调度逻辑，也没有运行 dsh 的测试或真实模型效果评测。上一轮单元测试通过说明已有测试覆盖的行为通过，不能用来证明以上任务语义已经完善。
