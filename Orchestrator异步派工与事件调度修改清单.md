# PepsiCode Orchestrator 待修改与待验收清单

更新日期：2026-11-17。目标是在用户提交一次任务后，协调者能派工、等待、接收失败、继续修复并验收，直到可交付或需要用户决策。既有实现和验收记录见 [阶段记录](docs/archive/orchestrator-plan-2026-11-16.md) 与 [逐项验收报告](reports/validation/orchestrator-checklist-2026-11-14.md#修复复验)。最初的四个断点见 [修复前的监督闭环实测](reports/validation/orchestrator-checklist-2026-11-14.md#监督闭环实测)。

## 优先修复本地监督流程

- [x] **结算 Worker 明确上报的失败。** Worker 正常结束模型回复时，根据最终结构化报告结算 `failed`、`blocked` 或 `dropped`，并把有界的失败证据投递给协调者。[Worker 结算](pepsicode/orchestration/worker.py#L226) · [失败分类与指纹](pepsicode/orchestration/repair.py#L24)
- [x] **让同一工作项接续已有提交。** 在已验证的失败重试场景中，新尝试沿用 `work_id`、worktree 和已有提交，携带失败证据并执行重测。结算时记录尝试与 HEAD 的对应关系。[重试](pepsicode/orchestration/store.py#L264) · [提交记录](pepsicode/orchestration/environment.py#L88)
- [x] **从组合测试失败的集成版本修复，并在交付目录验收。** 新派的修复 Worker 接续测试失败的集成 HEAD，保留原候选与失败证据。重新集成并交付后，父任务在已交付的 checkout 检查产物和验收条件。[修复基线](pepsicode/orchestration/environment.py#L165) · [父任务验收](pepsicode/core/task_verification.py#L25)

- [x] **将已知的 worktree 准备失败投递给协调者。** 可确认没有副作用的准备失败应生成归属于当前尝试的结果事件，让协调者决定下一步，并覆盖未受影响的派工继续运行。结果不明时保留现场并进入执行层核对。[准备异常](pepsicode/orchestration/runtime.py#L392)
- [x] **重试前核实 Worker 的提交归属。** 将 checkout 的当前 HEAD 与该工作项的已核实尝试记录对照，验证只有归属明确的提交可以接续。已补测无归属提交、已记录的合法提交与 HEAD 回退。[工作区检查](pepsicode/orchestration/environment.py#L57)
- [x] **恢复会话时复核实际交付的 SHA。** 在目录内容摘要之外，把 checkout 当前 HEAD 与验收记录的交付 SHA 对照。HEAD 不一致、checkout 已清理或版本不可确认时，将完成状态转为待复核并保留历史证据。已补测文件内容相同但 HEAD 改变以及 checkout 缺失的情况。[会话恢复](pepsicode/core/session.py#L387)

## 监督流程的通过条件

- [x] **同一工作项失败后修复。** 从常规入口提交一次任务。Worker 提交部分改动后，真实测试失败；协调者收到失败事件、安排新尝试接续该提交，执行重测并交付。验收必须证明同一 `work_id`、不同尝试 ID、原成果仍在、实际重测通过、当前版本的父任务验收通过，且过程中无需用户催促。正常运行时宿主等待事件。（离线回归 `tests/core/test_supervision_loop.py::test_failed_worker_repairs_retests_and_delivers`，含原始探针 `.evals/supervision-worker-20261116` 通过。）
- [x] **各候选通过、组合验收失败后修复。** 两个候选分别通过各自检查，集成后针对组合行为的检查失败。协调者基于失败版本安排修复，新的交付提交通过项目检查与独立验收。失败版本须留有证据且不能交付；不得修改测试或放宽验收条件来制造通过。（离线回归 `tests/core/test_supervision_loop.py::test_failed_integration_repairs_on_failed_baseline`，含原始探针 `.evals/supervision-integration-20261116` 通过。）
- [x] **实际 Git 合并冲突的修复验收。** 验证协调者能在保留原候选的情况下处理可修复的 Git 冲突，并对新的集成结果重新验收。（离线回归 `tests/core/test_supervision_loop.py::test_conflicted_integration_preserves_candidates_and_delivers_resolved_version` 通过。）
- [x] **真实模型复验。** 按授权预算，用真实模型从同一入口重复评测。记录有效完成、错误宣告完成、人工介入、请求数、耗时及失败原因。单 Agent 基准另行报告。（2026-11-17 使用 `deepseek-flash` 从 `run_verified_turn` 实测 `n=1`：编排 M2+M4 模式有效完成 1/1、错误宣告完成 0、人工介入 0、21 次请求、12,687 input / 5,116 output tokens、估算 \$0.145022、69.89s、集成 `accepted` 且本地交付 `local`；单 Agent 基准有效完成 1/1、错误宣告完成 0、人工介入 0、7 次请求、10,787 input / 1,252 output tokens、估算 \$0.067768、16.69s。）

## 后续工程与平台验收

- [x] **等待、恢复和多任务负载。** 补测满载时审批释放槽位、跨会话总容量、事件洪峰下的公平性与通知合并、三端只浏览旧会话时零模型请求。对长任务验证压缩后仍可追溯失败、派工与验收证据；在真实服务进程中覆盖在途尝试强杀重启、预算延续和未知副作用待核对。验证普通 Worker 失败仅影响自身，必答问题只阻塞依赖它的工作。（[tests/core/test_orchestration_load_and_recovery.py](tests/core/test_orchestration_load_and_recovery.py) 6 项通过；[runtime.py](pepsicode/orchestration/runtime.py) 增加 `maxTotalActiveWorkers` 宿主全局槽位池；[context_manager.py](pepsicode/context/context_manager.py) 增加 `_render_orchestration_recovery` 保留派工/失败/集成/交付证据。）
- [x] **低打扰和预授权边界。** 将工具及网络策略字段接入实际执行检查，或明确拒绝未支持的策略字段。验证策略被撤销、到期或收紧后，在下一副作用入口阻止越界操作。用真实 TUI、Electron、QQ 验收问题、必要审批、重连补取和单次交付通知。（内核策略与收紧门禁由 `tests/service/test_service_policy.py` 9 项通过；常驻服务单次交付摘要与未答问题发件箱去重由 `tests/server/test_server_service_mode.py` 17 项通过；TUI `test_service_console.py` 11 项、桌面 `pepsicode-client` 83 项与 typecheck、QQ 28 项单测与 2 项真实回环 E2E 全部通过。注：未接入真实外网 QQ 账号与人工点击真窗口。）
- [ ] **独立运行环境和 POSIX。** 完成依赖安装及共享缓存锁策略、可追溯的依赖和测试环境、端口分配及打包运行入口。实测 POSIX 锁、进程树收敛、恢复和交付；在支持平台验证 Linux Bubblewrap 的实际文件、进程与网络限制，并补齐或明确未支持的 CPU、内存、进程和磁盘等硬限额。（**本地已完成：** [environment.py](pepsicode/orchestration/environment.py) 补齐锁文件摘要 `lockfile_digests`、共享缓存锁 `own_dependency_cache`、受管依赖安装、不重叠 `port_range` 分配、显式 `resource_limits` 声明（`wall_time_seconds`/`ipc_frame_bytes`/`ipc_messages`/`process_tree` 强制；`cpu`/`memory_bytes`/`max_processes`/`disk_bytes` 明确标 `unsupported`）；[process_worker.py](pepsicode/orchestration/process_worker.py)、[main.py](pepsicode/main.py) 与 [cli.py](pepsicode/service/cli.py) 支持 `sys.frozen` 打包入口与端口/测试数据环境注入，`test_environment_manifest_ports_lockfiles_cache_lock_and_resource_limits` 通过。**待实测：** 本机无可用 Linux/WSL/Docker 守护进程，POSIX `fcntl` 锁、进程组与 Linux `bwrap` 隔离待在 Linux/POSIX 主机实测。）
- [ ] **外部交付及部署。** 在明确授权的测试仓库验证真实 GitHub 推送、PR、CI、审查事件及 QQ 通知。覆盖事件身份与任务范围、新旧 head、外部请求结果不明后的核对，以及交付目标与完成状态的对应。远程部署另验收真实运行主机、关机与恢复、访问控制和凭据轮换。（**本地已完成：** [task_verification.py](pepsicode/core/task_verification.py) 对齐 `pull_request` / `merge_or_deploy` / `uncertain` 与父任务完成门禁；[host.py](pepsicode/service/host.py) 为外部事件绑定交付 `head_sha` 并新增 `rotate_token`；[app.py](pepsicode/server/app.py) 暴露 `service/rotate_token` 动态切换认证令牌，`test_delivery_target_gates_parent_task_completion_and_routes_ci_review_and_token_rotation` 通过。**待实测：** 真实 GitHub 测试仓库推送/PR/CI 联调、外网 QQ 通知及远程运行主机跨机持久化与关机恢复。）
- [x] **Worker 现场保留与清理。** 验证保留期、工作区归属及唯一成果保护。待核对、副作用不明或仍有未交付改动的现场保留；可清理项仍走执行层。（离线回归 `tests/core/test_m2_environment.py::test_worker_environment_retention_and_pruning` 通过。）

## 2026-11-17 本地监督流程实施记录

| 项 | 实现位置 | 实际检查 |
| --- | --- | --- |
| Worker 如实结算 | [worker.py](pepsicode/orchestration/worker.py)（`_final_report`/`_failure_evidence`）、[repair.py](pepsicode/orchestration/repair.py)（`settled_evidence` 结构化分类与指纹） | `test_failed_worker_repairs_retests_and_delivers` 断言失败事件 kind=`failed`、证据 JSON 含 stage/class/check/output、无 `result_ready` 假结果 |
| 接续原成果修复 | [environment.py](pepsicode/orchestration/environment.py)（`_verify_checkout` 校验提交继承关系，`attribute_managed_output` 记录产出） | 同一 `work_id` 两次尝试，`head_updates` 记录尝试和 HEAD 的对应关系，原提交是最终 HEAD 的祖先。`test_failed_checkout_is_retained_and_not_replayed` 等既有准备与未知结果回归通过 |
| 集成失败修复与版本统一 | [environment.py](pepsicode/orchestration/environment.py)（`_repair_baseline`）、[task_verification.py](pepsicode/core/task_verification.py)（`delivered_acceptance_root`） | 修复 checkout 的 HEAD 等于失败集成 HEAD，且含全部候选文件。失败集成保留证据并拒绝交付。父验收记录 `delivered.{checkout,head_sha}`，即使主工作区没有产物也能完成。在线验收使用 `delivered_version_changed` 标记失效版本 |
| 准备失败事件投递 | [runtime.py](pepsicode/orchestration/runtime.py)（`_schedule` 准备异常处理） | `test_known_prepare_failure_delivers_event_and_keeps_sibling_running` 验证无副作用准备失败结算 `failed` 并投递事件且兄弟派工继续；`test_unknown_prepare_failure_preserves_state_and_blocks_scheduling` 验证 `has_unknown` 时保留现场并中断调度 |
| 重试提交归属核实 | [environment.py](pepsicode/orchestration/environment.py)（`_verify_checkout` 核对 `head_updates`） | `test_retry_checkout_verifies_commit_attribution` 验证未记录归属的后代提交、已记录合法提交与 HEAD 回退 |
| 恢复交付 SHA 复核 | [session.py](pepsicode/core/session.py)（`_read_git_head`、`load_session`） | `test_resume_rechecks_delivered_head_sha_and_missing_checkout` 验证内容相同但 HEAD 改变及 checkout 被清理时转为 `incomplete`/`needs_recheck` 并保留 `legacy_evidence` |
| Git 合并冲突修复与保留候选 | [integration.py](pepsicode/orchestration/integration.py)（`validate_integration` 保留 `conflict` 状态）、[test_supervision_loop.py](tests/core/test_supervision_loop.py) | `test_conflicted_integration_preserves_candidates_and_delivers_resolved_version` 验证真实 Git 冲突中止 cherry-pick、保留原候选与冲突证据、拒绝交付冲突集成、派发解决 Worker 合并双边改动并通过重测与父任务验收 |
| Worker 现场保留与清理 | [environment.py](pepsicode/orchestration/environment.py)（`prune_environment`/`prune_environments`）、[candidate.py](pepsicode/orchestration/candidate.py)、[schema.py](pepsicode/orchestration/schema.py) | `test_worker_environment_retention_and_pruning` 验证在途/失败/脏改动/未交付唯一成果及保留期内拒绝清理，已交付且过保留期的环境经执行层清理并保留候选/集成有效性，同时验证无副作用准备失败可按原 `work_id` 重试准备 |
| 预授权策略字段与收紧门禁 | [policy.py](pepsicode/service/policy.py)（`evaluate_request`/`check_policy_checkpoint`）、[runtime.py](pepsicode/execution/runtime.py)（`run_policy_checkpoint`）、[tooling.py](pepsicode/tooling.py)、[filesystem.py](pepsicode/execution/filesystem.py)、[process.py](pepsicode/execution/process.py)、[external.py](pepsicode/execution/external.py) | `test_tool_network_and_external_action_policy_fields` 与 `test_policy_tightening_revocation_and_expiry_block_next_side_effect` 验证工具、路径、命令、网络目标（含重定向）与外部动作策略在下一副作用入口生效 |
| 服务单次交付与问题通知 | [app.py](pepsicode/server/app.py)（`_record_service_turn_notifications`） | `test_service_emits_single_delivery_and_question_notifications_on_turn_end` 验证未答问题通知与带版本/证据/保留环境摘要的单次交付通知去重入箱 |
| 真实模型编排提示与复验 | [interactive.py](pepsicode/orchestration/interactive.py)（`_ROLE`）、[task.py](pepsicode/tools/task.py)（worktree 提交/交付指引） | `deepseek-flash` 从 `run_verified_turn` 实测（`n=1`）：编排模式 21 次请求/69.89s 有效完成并集成交付；单 Agent 基准 7 次请求/16.69s 有效完成；`tests/context/test_harness_instructions.py` 等 97 项回归通过 |
| 等待、恢复与多任务负载 | [config.py](pepsicode/config.py)（`maxTotalActiveWorkers`）、[runtime.py](pepsicode/orchestration/runtime.py)（`_try_acquire_host_slot`/`_release_host_slot`/`_wait_for_host_slot`）、[context_manager.py](pepsicode/context/context_manager.py)（`_render_orchestration_recovery`/`observe_tool_result`） | [tests/core/test_orchestration_load_and_recovery.py](tests/core/test_orchestration_load_and_recovery.py) 6 项通过，覆盖满载审批槽位释放、跨会话总容量、事件洪峰合并/公平性与失败/问题隔离、TUI/Desktop/QQ 只读浏览零模型请求、长任务压缩后证据保留，以及真实子进程强杀重启后的 `recovery_pending` 栅栏与日志预算恢复 |
| 独立环境清单、端口/缓存锁与打包入口 | [environment.py](pepsicode/orchestration/environment.py)（`own_dependency_cache`/`_lockfile_digests`/`_allocate_port_range`/`_resource_limits`）、[process_worker.py](pepsicode/orchestration/process_worker.py)、[worker.py](pepsicode/orchestration/worker.py)、[main.py](pepsicode/main.py)、[cli.py](pepsicode/service/cli.py)、[manage_cli.py](pepsicode/cli/manage_cli.py) | `test_environment_manifest_ports_lockfiles_cache_lock_and_resource_limits` 验证锁文件摘要、共享缓存互斥锁、受管依赖安装、非重叠端口分配、硬/未支持限额声明及 `sys.frozen` 下 `pepsicode service ...` 与 Worker 启动链路 |
| 交付目标门禁、外部事件 HEAD 绑定与凭据轮换 | [task_verification.py](pepsicode/core/task_verification.py)（`delivered_acceptance_root`）、[host.py](pepsicode/service/host.py)（`_owns_external_event`/`rotate_token`）、[app.py](pepsicode/server/app.py)（`service/rotate_token`） | `test_delivery_target_gates_parent_task_completion_and_routes_ci_review_and_token_rotation` 验证本地/PR/合并目标完成门禁、`uncertain` 阻塞、新旧 `head_sha` 事件筛选，以及 WebSocket 在线轮换令牌后拒绝旧令牌、接受新令牌 |

验证（Windows 11, Python 3.12.4，当前工作区）：

- `python -m pytest -q --durations=5 --basetemp=.pepsi-code/pytest-tmp8 tests/core/test_m2_environment.py tests/core/test_m2_process_channel.py tests/core/test_m4_delivery.py tests/core/test_m4_external_delivery.py tests/server/test_server_service_mode.py tests/service/test_connectors_github.py tests/server/test_desktop_runtime.py`：**70 通过**。
- `python -m pytest -q --basetemp=.pepsi-code/pytest-tmp8 tests/core/test_orchestration_load_and_recovery.py tests/core/test_orchestration_config.py tests/core/test_orchestration_store.py tests/context/test_context_compaction.py`：**72 通过**。

- `python -m pytest -q --basetemp=.pepsi-code/pytest-tmp8 tests/context/test_harness_instructions.py tests/core/test_subagent_contract.py tests/core/test_orchestration_store.py tests/core/test_supervision_loop.py tests/server/test_server_service_mode.py tests/service/test_service_policy.py tests/tui/test_service_console.py`：**97 通过**。
- 桌面端 `pepsicode-client/`：`npm test`（**83 通过**）、`npm run typecheck` 通过；QQ 通道 `integrations/qq/`：`npm test`（**28 通过**）、`npm run test:e2e`（**2 通过**）。
- `python -m pepsicode.evals --mode smoke`：**smoke_passed=true**（1 通过、0 基础设施错误）。
- `python -m pytest -q --basetemp=.pepsi-code/pytest-tmp8 tests/core/test_m2_environment.py tests/core/test_verification_resume.py tests/core/test_supervision_loop.py tests/core/test_m4_delivery.py tests/core/test_session.py tests/execution/test_execution_boundaries.py tests/execution/test_execution_recovery.py`：**85 通过、1 跳过**（POSIX PID 探针）。
- `python -m pytest -q tests/core/test_supervision_loop.py`：3 项通过（三条离线通过条件，含 Git 合并冲突修复验收）。
- 原始复现探针 `python -m pytest -q .evals/supervision-worker-20261116/test_worker_supervision.py .evals/supervision-integration-20261116/test_integration_supervision.py`：4 项通过（修复前在同一断言上失败）。
- 2026-11-17 独立复核同时复跑上述 2 项端到端测试和 4 项原始探针，6 项通过。
- 定向回归 `tests/core`（编排存储、M2 环境/进程、M4 交付/外部交付、任务编排、验收恢复、会话）+ `tests/execution`（边界、恢复、预算）+ 子 Agent 状态/契约：合计 259 项通过、1 项跳过（POSIX PID 探针）。
- 全量 `python -m pytest -q --basetemp=<工作区临时目录>`：**1,493 通过、0 失败、6 跳过**（跳过项为 Linux Bubblewrap、POSIX PID、系统账户限制与真实 API 用例）。
- `python -m ruff check pepsicode tests`、`python -m ruff format --check pepsicode tests`、`python scripts/check_project.py` 通过。
- 文档同步：[features.md](docs/guides/features.md) 交付闭环与 Worker 状态段落、[task-orchestration.md](docs/architecture/task-orchestration.md) 重试与结算段落。

剩余问题与边界：

- `dropped` 报告按失败结算，其语义展示沿用现有状态投影；真实模型复验目前完成 `n=1` 的编排全链路与单 Agent 基准对比，多轮真实模型冲突/失败修复压力测试与配置 `maxIterations=1` 的复杂任务节奏待后续扩展样本观察。
- 桌面端与 QQ 通道已跑通各自自动化测试与回环 E2E；真机桌面窗口点击与真实外网 QQ 账号人工验收未执行。
- 独立运行环境的 POSIX `fcntl`/进程组/Linux Bubblewrap 与外部交付的真实 GitHub 测试仓库/远程主机部署受限于本机无可用 Linux（无已安装 WSL 发行版、Docker 守护进程未运行）与无外部测试仓库/主机凭据，保留 `[ ]` 待对应环境实测后勾选。

