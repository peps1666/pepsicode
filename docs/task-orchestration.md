# 用户任务的生命周期与验收

这次改动参考了本机 dsh 的输入、轮次、目标与续行边界，沿用 Pepsicode 的 Python 执行器和有界验证循环。源码比较和改造前的离线探针见[审查记录](task-orchestration-review.md)。

## 从用户输入到任务结束

1. 每条用户输入有稳定的来源 ID。运行中的 `steer` 修改当前任务；`follow_up` 在当前任务通过验证后作为下一个独立任务执行。
2. 用户任务有独立的 ID、修订号、目标、验收条件和状态。一次用户任务可能经历多次模型请求、工具批次和验证重试；执行轮次结束不等于任务完成。
3. 新指令一经队列持久化接受，就增加当前任务修订号并清空旧证据。模型收到指令后才更新已交付修订号。验证必须对应同一个、已交付的修订号。
4. 项目回归检查和任务验收分别记录。只给计划、项目测试全绿、无文件变更、模型输出“完成”都不能替代操作类任务的验收条件。
5. 验收失败沿用有界修复：`maxIterations` 限制总轮数，`verificationNoProgressLimit` 限制重复失败；同时受执行取消、请求预算和费用预算约束。达到限制保持未完成。

任务类型包括 `answer`（解释、评审等回答）、`change`（实现或修改）和 `operation`（执行操作）。入口用文本规则给出初始分类；这只是调度提示，无法覆盖所有自然语言。程序调用方可明确传 `task_kind`；模型可通过 `task_state` 将回答提升为操作类任务，不能将操作类任务降级来跳过验收。即使类型为回答，显式提供的验收条件也必须检查。

## 可复核的验收条件

`task_state` 始终向主代理提供。`inspect` 返回任务和待回答问题；`define` 必须携带准确的 `task_id` 和 `revision`，只允许追加条件。条件包括：

| 条件 | 检查内容 | 证据 |
| --- | --- | --- |
| `file_exists` | 工作区内指定文件存在 | 路径、内容版本 |
| `file_contains` | 文件包含指定文本 | 路径、内容版本、是否匹配 |
| `command` | 指定 argv 的进程成功退出 | argv、退出码、输出摘要与哈希 |

文件条件必须使用工作区相对路径。命令条件经过已有命令权限、Hooks、执行日志和取消路径，单条超时 30 秒。Python 字节码缓存按验收隔离，避免重复检查执行旧缓存。验收命令不会因为写在条件里而获得额外权限。

更新条件采用修订号比较；旧版本写入被拒绝。新用户指令使旧条件需要重新确认，不能直接沿用旧证据。若需求确实变化，`revise` 必须引用最新真实输入的 `instruction_id` 并说明原因，保存旧条件与修订依据。单纯“继续”不授权删除原条件。修改后的条件仍由模型解释用户意图，Harness 不能证明替换条件完整表达了用户的新要求。

验收结束前再次核对任务版本及已读文件版本。检查中途出现用户新指令、文件变化或未知执行结果，不提交成功证据。当前任务仍有未完成 Todo 或必答问题，也不能完成。

这些机制保证的是声明的条件确实执行并与相应任务版本关联。**条件是否覆盖全部需求，仍取决于调用方、模型和测试质量。** `file_contains` 适合产物检查；行为变更应优先使用针对行为的命令测试。文件存在和无关测试通过都不能证明功能正确。

程序调用示例（其余参数与原有 `run_verified_turn` 相同）：

```python
run_verified_turn(
    runtime=runtime,
    runner=run_agent_turn,
    model=model,
    tools=tools,
    messages=[{"role": "user", "content": "修复分页边界"}],
    cwd=workspace,
    permissions=permissions,
    task_kind="change",
    acceptance=[{
        "id": "pagination-behavior",
        "kind": "command",
        "argv": ["python", "-m", "pytest", "tests/test_pagination.py", "-q"],
    }],
)
```

WebSocket `turn/run` 同样支持 `task_kind`、`acceptance` 和 `answer_to`（问题 ID 数组）。CLI/TUI/桌面共用这个执行入口。内嵌调用若要跨进程恢复，应使用 `recording_session(session)` 绑定持久会话；没有会话时只在 runtime 中维护状态。`runtime.taskAcceptance` 可设置调用环境的默认条件，多个不同任务应优先逐次传入 `acceptance`。

## 输入恢复与澄清

原生入口将队列绑定到 `SessionData.task_control`。输入先落盘，再通知已接受；存储失败会回滚内存状态，调用方收到错误。交付采用 pending → claimed → 写入消息树 → acknowledge：崩溃后未进入消息树的 claimed 恢复为待交付，已进入消息树的输入按来源 ID 去重。

被移除或在停止时尚未处理的输入保存为 returned，界面将其退回草稿。恢复会话不会自动启动模型，也不赋予历史任务无限续行权限。下次显式运行才会继续处理恢复的队列；returned 可再次回显以避免丢失草稿。输入交付去重不等于外部副作用恰好执行一次，未知副作用仍须经过[执行恢复](harness-boundaries.md)核对。

Todo 和问题归属各自会话；子代理使用独立控制状态，不能修改父任务验收。Todo 有稳定 ID、完成状态和所属任务 ID。`ask_user` 默认 `required: true`，任务进入 `waiting_user`；`required: false` 可提出不阻塞的问题。回复一个待答问题时可自动关联；多个待答问题应通过 `answer_to` 明确关联，系统不会用超时编造回答。

未完成任务收到“继续”，或等待澄清时收到回复，会保留任务 ID 并增加版本。普通新任务及 `follow_up` 创建新 ID，旧任务保存到历史。切换对话分支清除当前任务身份；复制或 fork 不复制任务续行身份和 inbox。重新生成同一任务仍必须重新验收，不能继承上一次成功结论。对话分支共用磁盘文件，不恢复历史文件状态。

## 结束原因与防空转

普通与流式模型响应都检查停止原因。`max_tokens`、`pause_turn`、拒答和缺少完整结束事件不会被当作最终成功，也不派发该响应里的工具。已有的空推理输出重试仍有次数上限；非空的半截结果只作为过程保留。流式响应即使出现 `end_turn`，缺少最终 `message_stop` 仍视为不完整。

同一执行范围内，相同任务版本、工具、参数和结果连续重复时，默认在第 3、5、8 次附加提醒，要求改变方法或说明阻碍。可设置 `repeatToolThresholds: [3, 5, 8]`，必须为递增且不重复的整数，最小为 2。提醒不扩大权限，也不替代预算和修复轮数限制。

任务状态为 `active`、`answered`、`complete`、`incomplete`、`waiting_user`、`cancelled`、`blocked`、`unknown` 或 `budget_exhausted`。桌面轨迹显示任务状态和已满足条件数，服务端 `turn/end` 返回任务报告；执行成功与用户任务未完成可以同时存在。

当前实现提供有界重试、显式继续和持久恢复，没有新增跨进程无人值守的无限目标驱动器。离线编排回归见 `tests/test_task_orchestration.py`；真实任务评测入口和 smoke/real 统计边界见[任务评测](task-evaluations.md)。
