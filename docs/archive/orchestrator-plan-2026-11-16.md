# PepsiCode Orchestrator、异步派工与事件驱动调度修改清单

编写日期：2026-11-11。

本版已按“面向真实软件工程的长期运行系统”扩展目标。原清单的进程内异步协调只构成 M1；独立工作区与进程隔离 M2、常驻服务与低打扰协作 M3、工程交付闭环 M4 均纳入本次规划。分阶段实现不等于可以省略后续能力。

本文供开发人员实施与验收。已有状态追踪能力见 [修改清单.md](../../修改清单.md)。完成一项必须附代码位置和实际验证记录，不得仅根据提示词或界面截图勾选。

开发前阅读 [开发约定](../../docs/development/development.md)、[Harness 执行边界](../../docs/architecture/harness-boundaries.md)和[任务生命周期与验收](../../docs/architecture/task-orchestration.md)。

### 2026-11-12 实施记录

M1 保留以下三项未完成验收，M2 实施期间继续单独记录：

- [ ] POSIX 实机验收，包括工作区锁、进程树停止及中断恢复。
- [ ] TUI、Electron 桌面和 QQ 启用 M1 后的人工操作验收。
- [ ] 真实模型的派工、结果驱动续行及父任务验收。

M1 已接入三端共用的交互入口，默认关闭。以下按实际实现和验证记录；M2–M4 不在本轮范围内。

| 范围 | 实现与离线证据 |
| --- | --- |
| A03 的宿主与脚本模型 | [宿主调度](../../pepsicode/orchestration/runtime.py)在第一项结果已提交、第二项仍运行时启动协调回调。[回归用例](../../tests/core/test_orchestration_store.py)通过主循环读取结果并调用派工工具，验证后续工作先于第二项结束。 |
| A02 的实际子 Agent 路径 | [Worker 执行器](../../pepsicode/orchestration/worker.py)复用子 Agent 主循环和 `agent_report`。脚本模型在兄弟 Worker 仍运行时产生必答问题，协调回调读取已持久化的问题。验证同一派工追踪 ID、同一日志及共享请求计数。 |
| A10、A24 的托管边界 | [宿主调度](../../pepsicode/orchestration/runtime.py)观察根执行范围的停止信号，禁止新的 Worker 启动和协调唤醒，保存未启动项的取消状态。[进程执行层](../../pepsicode/execution/process.py)拒绝托管 Worker 的持久后台命令。 |
| A04、A05 的宿主工具与存储 | [派工工具](../../pepsicode/orchestration/coordinator.py)、[派工存储](../../pepsicode/orchestration/store.py)和[回归用例](../../tests/core/test_orchestration_store.py)覆盖同一键重复十次、冲突参数及原子保存失败。工具已接入显式启用的宿主回调。 |
| 协调轮次状态查询 | [状态工具](../../pepsicode/tools/agent_status.py)在有编排记录的会话中提供当前父任务的分页状态，沿用现有追踪查询。回归验证两次派工已返回且执行尚未开始时可见两条排队记录。 |
| A01、A12 的共享交互入口 | [交互入口](../../pepsicode/orchestration/interactive.py)与[验证入口](../../pepsicode/core/verification.py)串起三端共用主循环。脚本模型验证旧 Worker 尚未结束时，新用户指令已唤醒协调者；旧结果按修订归档。 |
| A11、A13 的控制 | [协调工具](../../pepsicode/orchestration/coordinator.py)与[状态存储](../../pepsicode/orchestration/store.py)提供局部停止和新尝试。测试覆盖停止目标而兄弟执行继续、定向回复只匹配对应 Worker。桌面状态卡及 TUI、QQ 命令经同一取消路径。 |
| A16、A19 的恢复 | [预算恢复](../../pepsicode/orchestration/budget.py)使用提供方请求日志重算已用额度、费用及不确定标记。测试覆盖显式继续后关闭中断尝试、已领取尝试不重放和 Worker 保留协调收尾额度。 |
| A18 的父任务验收 | [共享验证回归](../../tests/core/test_orchestration_store.py)验证同一 Worker 的“完成”文本在目标文件缺失时无法使父任务通过，目标文件满足条件时由父验收确认。 |
| A06、A07 的宿主恢复 | [宿主调度](../../pepsicode/orchestration/runtime.py)恢复多批已提交结果；存在未核对领取时，宿主暂停且存储层拒绝新领取。[派工回执恢复](../../pepsicode/orchestration/recovery.py)只确认身份和参数与持久记录一致的派工。[回归用例](../../tests/core/test_orchestration_store.py)覆盖工具回执丢失后沿用原尝试、有日志而无持久回执时继续阻塞、已结束尝试不重跑及已提交结果分批补投递。 |
| A09 的宿主排他 | [宿主调度](../../pepsicode/orchestration/runtime.py)按会话工作区真实路径持有独占锁，测试覆盖同一工作区的第二会话、另一进程及释放后重新获取。共享同一临时目录的编排宿主参与锁协议。 |
| A12 的队列与结果归档 | [派工存储](../../pepsicode/orchestration/store.py)按当前任务及修订计算排队容量；[回归用例](../../tests/core/test_orchestration_store.py)覆盖旧队列跳过和迟到结果保留但不唤醒协调者。 |

Windows 上桌面单测 79 项、QQ 单测 27 项及本机 WebSocket 端到端测试 2 项通过，桌面类型检查和构建通过。沙箱内的临时目录替换、子进程与回环连接故障在宿主复测中解除。POSIX、三端实际启用编排后的人工交互及真实模型未验收。

Windows 全量 Python 回归记录 1,364 项通过、4 项跳过、3 项失败。对同一 HEAD 的独立源码副本复跑，Token 遥测和 TUI Plan 状态栏的断言失败可复现；`pylsp` 初始化在全量运行中超时，在 HEAD 单项与当前工作区定向复测均通过。M1、子 Agent、执行边界、预算、恢复、服务端和 TUI 侧栏等定向回归合计 151 项通过、1 项跳过。Ruff、格式、项目检查与离线 smoke 通过。

宿主验收复现步骤：

1. 执行 `python -m pytest -q tests/core/test_orchestration_store.py -k "question_is_delivered or wakes_for_steering"`。观察 Worker A 的结果或定向回复先进入协调轮次，测试随后才解除 Worker B 的同步屏障。
2. 执行 `python -m pytest -q tests/core/test_orchestration_store.py -k "restart or interactive_continue"`。观察已提交结果补投递、未核对领取不重放，以及显式继续时关闭已核对的旧尝试。
3. 在 `integrations/qq` 执行 `npm run test:e2e`。M1 用例通过真实本机 WebSocket 向正在协调的会话提交定向回复。兄弟 Worker 在回复提交时仍在运行，持久快照记录答复 ID、新尝试及旧兄弟结果归档。该用例使用脚本模型。

### 2026-11-14 M2 实施记录（第六批，补记）

M2 实现早于本记录；本批对当前工作区复跑验证并补记实施位置与证据。隔离等级按第 18.1 节口径：当前仅达到 workspace（进程/工作区隔离），hardened 安全隔离未实现，交付停留在该等级，不宣称系统级安全隔离。

| 范围 | 实现与离线证据 |
| --- | --- |
| worktree/分支环境准备 | [环境准备](../../pepsicode/orchestration/environment.py)以调用方显式提供的已提交 `base_sha` 建 `codex/m2-<work_id>` 分支 worktree（`git worktree add --no-track -b`），目录 `.pepsi-code/m2/<work_id>/{checkout,runtime}`；manifest 持久化 repository/base_sha/expected_head/branch/worktree/目录清单/运行修订/隔离等级，状态机 `prepare_pending → preparing → prepared/prepare_failed`，失败保留现场。基线提交含 `.env`/`settings.local.json` 拒绝；受管分支已存在拒绝且不留半成品目录；`git worktree` 响应丢失时进入 prepare_failed、重放被 Reconcile 挡住（未知结果不盲目重做）。重入先比对 manifest 身份再复验顶层、common dir、分支与 HEAD。 |
| 进程通道 | [进程通道](../../pepsicode/orchestration/process_channel.py)经执行层启动可信子进程；帧协议含 run/attempt/generation/nonce 身份、严格递增序号、64KiB 帧上限与 256 条消息上限，拒绝 NaN/Infinity 与深递归。无效身份、超限帧、错序、未注册操作均报 `WorkerProtocolError` 且宿主操作回调不执行、预算不计费。无心跳帧；死亡判定用 deadline（与 scope.deadline 取 min）而非文字输出静默。 |
| Worker 进程模型 | [Worker 进程](../../pepsicode/orchestration/process_worker.py)以 `[sys.executable, -I, -u]` 启动无特权子进程（[子进程侧](../../pepsicode/orchestration/process_worker_child.py)只做帧收发与操作请求）；模型调用与工具执行由宿主仲裁。环境变量白名单（PATH/SystemRoot/WINDIR/SystemDrive/PATHEXT）加隔离 `TMP/TEMP/TMPDIR`、`PIP_CACHE_DIR`、`npm_config_cache`、`PYTHONPYCACHEPREFIX`，分别指向 runtime 下的 tmp/dependencies/build。 |
| 预算代理 | 模型请求只发生在宿主进程：`next_model` 返回后立即计量 token/费用并写请求日志；Worker 强制共享 `scope.budget`，协调收尾预留由 `check_worker` 扣除。回归验证实际 provider 请求次数 == `budget.requests`，`model/prepared` journal 记录的 body 与实际请求逐字节一致且 API key 不落 journal。 |
| 崩溃收敛与收割 | monitor 线程在宿主操作执行期间轮询子进程退出：单 Worker 崩溃（exit 7/9）置位自身取消、中断挂起的宿主操作、该 attempt 结算为 failed（含退出码），兄弟 Worker 经 Barrier 同步继续并照常计量共享预算，worktree 与成果保留。定向停止与 deadline 超时在返回前收割子进程并写 `orchestration/worker_process_ended`（含 exit_code）。 |

未实现/未验收（与第 18 节叙述一致，如实标注）：

- 隔离等级仅 workspace；hardened（OS 后端文件/网络/资源限制）未实现，`isolation="hardened"` 在任何 git/进程调用前即拒绝，不得标作安全隔离。
- CPU/内存/进程数/磁盘/网络硬限制未实现；当前限制仅为帧大小/消息数上限与 deadline。端口自动分配未实现。
- POSIX 平台未实测（沿用 M1 未验收记录）。

验证记录（Windows 11, Python 3.12.4，2026-11-14 当前工作区）：

- `python -m pytest -q tests/core/test_m2_environment.py tests/core/test_m2_process_channel.py --basetemp=<独立临时目录>`：15 项通过（真实临时 Git 仓库与真实 Python 子进程，仅模型层打桩）。覆盖 B01（两个 worktree 并行写同名 `target.txt` 且主工作区脏文件不污染）、B03（单 Worker 崩溃兄弟继续）、B07（分支碰撞、准备失败不重放）、无效帧不触达宿主操作、定向停止收割、越界写文件被拒、子进程环境无注入密钥。
- `python -m pytest -q --basetemp=<独立临时目录>`（全量）：1,450 通过、5 跳过、2 失败——`test_usage_telemetry` 流式遥测与 `test_tty_app` TUI Plan 状态栏，与 M1/M3 记录中同一 HEAD 可复现的既有失败一致。注：本机默认 pytest 临时根 `pytest-of-Administrator` 出现 `WinError 5` 拒绝访问（环境问题，非代码问题），全量复现需指定独立 `--basetemp`。
- `python -m ruff check pepsicode tests`、`python -m ruff format --check pepsicode tests`、`python scripts/check_project.py`、`python -m pepsicode.evals --mode smoke` 通过；桌面 `npm test` 83 项通过、`npm run typecheck` 通过；QQ `npm test` 通过、`npm run test:e2e` 2 项通过。

### 2026-11-13 M3 实施记录（第七批）

服务模式是同一内核的另一种运行所有权，不另建调度器。以下按实际实现与验证记录。

| 范围 | 实现与离线证据 |
| --- | --- |
| 服务宿主与 attach/detach | [服务宿主](../../pepsicode/service/host.py)持有驻留会话注册表与 attach 计数；[服务端改造](../../pepsicode/server/app.py)的 service 模式下 `session/create`/`session/resume` attach 到驻留会话，`_detach_session` 只解除订阅——不设取消事件、不拒绝待审批、不销毁工具。同一会话第二个控制连接被拒绝。回归 [test_server_service_mode.py](../../tests/server/test_server_service_mode.py) 验证断开后轮次继续、重连读到结果。 |
| B08 关闭客户端任务继续 | 集成测试 `test_b08_disconnect_does_not_cancel_and_reconnect_reads_journal`：客户端在模型阻塞期间断开，轮次无取消、Worker 持续活跃，完成后重连客户端经快照历史与事件日志读到 message/end 与 turn/end。交互模式断开仍取消的既有回归未回退（`test_disconnect_stops_worker_before_disposing_tools_and_prevents_later_writes`）。 |
| 持久事件日志与游标重连 | [事件日志](../../pepsicode/service/events.py)按会话追加 JSONL，单调序号，崩溃残行容忍，写失败抛 `EventLogError` 并置存储健康分项故障、暂停新轮次。[回归](../../tests/service/test_events_outbox.py)覆盖游标增量重放与写失败恢复；[服务端](../../pepsicode/server/app.py) `session/events` 与 `connection/ready` 的事件游标、`session/detach` 均有测试。 |
| 停止语义三分离 | `service/shutdown`（[CLI stop](../../pepsicode/service/cli.py) 经鉴权 RPC）写入人工停止标记并优雅收敛；标记存在时普通 `start` 被拒绝（B10），`start --clear-stop` 人工授权后可重启。`turn/cancel` 与断开语义不变。[回归](../../tests/server/test_server_service_mode.py)覆盖 shutdown 标记与守护器阻止；真实进程生命周期演示（start→health→status→stop→阻止→clear-stop→stop）通过。 |
| 单实例与运行代次 | [服务状态](../../pepsicode/service/state.py)持久化 PID、代次、令牌指纹；存活 PID（Windows `OpenProcess`，避免 `os.kill` 误杀）拒绝二次启动；旧代次在编排存储层的代次围栏下不能结算新宿主的领取。 |
| 预授权策略 | [策略模块](../../pepsicode/service/policy.py)严格校验（未知字段报错）、前缀命令/路径范围、到期与撤销在每次检查点从磁盘重载。[审批后端](../../pepsicode/server/app.py)策略命中自动允许并记录，未命中转交在线控制端，无人接入时拒绝并写入合并通知。[回归](../../tests/service/test_service_policy.py)覆盖撤销/到期/越界；service 模式 `turn/run` 无有效策略被拒绝有测试。 |
| GitHub/CI 连接器 | [连接器协议](../../pepsicode/service/connectors/base.py)持久外部事件 ID 去重、退避、来源允许列表；[GitHub 连接器](../../pepsicode/service/connectors/github.py)只读轮询 PR 审查与 check runs，绑定 head SHA，令牌仅从环境变量读取。[回归](../../tests/service/test_connectors_github.py)用假 HTTP 端点覆盖去重、跨重启持久、旧 SHA 成功不覆盖新 SHA、陌生仓库拒绝。事件只作为 steer 输入或暂存待处理，不自行启动轮次、不新增授权。 |
| 通知发件箱 | [发件箱](../../pepsicode/service/outbox.py)按 dedup_key 去重、状态 pending/uncertain、不自动重发；`notifications/fetch {cursor}` 供重连补取。[QQ 桥接](../../integrations/qq/src/bridge.js)记录服务代次并在启动时补取离线通知，[单测](../../integrations/qq/tests/bridge.test.js)验证代次跟踪与一次性补取。 |
| 健康分项 | [健康快照](../../pepsicode/service/host.py)分项存储/队列/模型/Worker/连接器/通知，周期写 `health.json` 供 CLI 无连接读取；事件写失败暂停新工作有回归。 |
| B09 重启恢复 | 服务重启后 attach 时：策略有效且 effect 全部 pending → 驱动与用户“继续”相同的续行路径（测试验证后台续行轮次启动且用户消息为“继续”）；[启动自主恢复](../../pepsicode/server/app.py) `_startup_auto_resume` 在无任何客户端时扫描本工作区持久会话并按同样条件顺序无头续行，已领取/待核对不自动重放并写入合并通知。[回归](../../tests/server/test_server_service_mode.py) `test_startup_auto_resume_without_any_client` 验证无客户端续行与待核对通知。预算恢复沿用 M1 `bind_budget(recovery=True)`。 |
| 桌面端 service 接入 | [主进程](../../pepsicode-client/electron/main.ts)支持 `PEPSICODE_SERVICE_URL` + `PEPSICODE_SERVICE_TOKEN_FILE` 直连常驻服务（不生成、退出时也不终止自有后端，关闭窗口仅断开控制端）。[渲染层](../../pepsicode-client/src/components/ServiceBanner.tsx)依据 `connection/ready` 服务标识显示代次、待处理通知计数与“关闭窗口不会停止任务”；通知计数经 `notifications/fetch`，turn/end 后刷新。[测试](../../pepsicode-client/tests/service-banner.test.cjs)覆盖服务标识记录、普通后端不误显示、turn/end 后补取。 |

验证记录（Windows 11, Python 3.12.4）：

- `python -m pytest -q tests/server tests/service tests/core/test_orchestration_store.py`：103 通过、1 跳过；`tests/execution tests/server tests/service tests/core` 组合复跑 503 通过、2 跳过。
- `python -m pytest -q`（全量，四次）：1,422/1,421/1,421/1,422 通过、5 跳过。固定失败为 `test_usage_telemetry` 流式遥测与 `test_tty_app` TUI Plan 状态栏（与 M1 记录中同一 HEAD 可复现的既有失败一致）；`test_execution_boundaries.py::test_allow_once_never_authorizes_another_command_or_cwd` 在三次全量中出现两次、单项与组合复测均通过，属全量并行下的环境性偶发，复现方法为全量运行后单独复跑该文件核对。
- `python -m ruff check pepsicode tests`、`python -m ruff format --check pepsicode tests`、`python scripts/check_project.py`、`python -m pepsicode.evals --mode smoke` 通过。
- 桌面 `npm test` 83 项通过、`npm run typecheck` 与 `npm run build` 通过；QQ `npm test` 28 项通过、`npm run test:e2e` 2 项通过。启动自主恢复与水合重构后定向回归 `tests/server tests/service tests/core tests/integrations` 共 453 项通过、1 项跳过。
- 服务生命周期实机演示：`python -m pepsicode.service start/health/status/stop` 全链路通过，人工停止标记阻止普通重启。

### 2026-11-14 M3 补充记录：TUI attach 入口

| 范围 | 实现与离线证据 |
| --- | --- |
| TUI attach 控制台 | [attach 控制台](../../pepsicode/tui/service_console.py) 经现有 WS 协议（`connection/auth` → `session/resume` → 事件游标）attach 驻留会话：显示服务代次、执行模式与待处理通知；`/sessions`、`/attach <id|latest>`、`/status`、`/notifications`、`/cancel`、`/detach` 提供查询与控制；普通文本在轮次空闲时经 `turn/run` 开始新轮次（服务端策略拒绝时如实显示，排队输入与取消不受影响），运行中自动转为 steer 排队；审批请求经 `tool/approve` 行内回答。退出控制台即 `session/detach`，不取消运行中轮次、不触碰 `service/shutdown`。入口为 [服务 CLI](../../pepsicode/service/cli.py) 新增 `attach` 子命令（复用 `service.json`/token 解析，支持 `PEPSICODE_SERVICE_URL`/`PEPSICODE_SERVICE_TOKEN_FILE` 覆盖）。回归 [test_service_console.py](../../tests/tui/test_service_console.py)：10 项单元测试（假连接覆盖横幅、命令、审批回答、Ctrl+C 取消、detach 语义）+ 1 项真实回环集成——真实 `websockets.serve` 起服务端，验证 attach 后 steer 送达运行中轮次、控制台退出 detach 后轮次不取消、重连经快照读到结果（TUI 层 B08 证据）。进程内 TUI 主路径未改动。 |

验证记录（Windows 11, Python 3.12.4）：

- `python -m pytest -q tests/tui/test_service_console.py`：11 项通过（含真实回环集成）。
- `python -m pytest -q tests/server tests/service tests/tui`：350 通过、1 跳过、1 失败——`test_tty_app` TUI Plan 状态栏断言（与 M1/M3 记录中同一 HEAD 可复现的既有失败一致，本次改动未触及 `tui/app.py`，单项复跑可复现同一空白字符差异）。
- `python -m pytest -q tests/core/test_orchestration_store.py`：32 通过。`python -m ruff check pepsicode tests`、`python -m ruff format --check pepsicode tests`、`python scripts/check_project.py`、`python -m pepsicode.evals --mode smoke` 通过。
- 实机演示（真实服务进程）：`python -m pepsicode.service start` → `python -m pepsicode.service attach`：控制台显示服务代次与待处理通知，`/status` 读取分项健康，`/sessions` 列出会话，`/detach` 退出且进程返回 0；随后 `stop` 正常停止（人工停止标记生效）。轮次运行中断开与重连读取由真实回环集成测试覆盖（脚本模型，不消耗真实模型请求）。POSIX 服务管理沿用未验收记录。

未验收/剩余问题：

- 真实 GitHub 凭据与真实 CI 事件未验证（假端点测试）；真实 IM 无人看护长期演示未执行。
- 远程主机部署、服务账户、访问控制与凭据轮换仅有文档说明，未实测；服务与执行同主机，主机离线任务同样中断。
- 连接器事件当前只进入活跃协调轮次或暂存待处理，服务不自行因外部事件发起新任务轮次（已接受任务的续行除外）；POSIX 上的服务管理未实测（沿用 M1 POSIX 未验收记录）。
- 桌面端已支持接入常驻服务并显示服务状态。TUI 已提供 `python -m pepsicode.service attach` 终端控制台作为 attach 入口（行式界面：查询、steer、取消、审批、退出即 detach，见 2026-11-14 补充记录）；完整 TUI（全屏交互）仍为进程内交互宿主，未改造为经服务驱动轮次。

### 2026-11-14 M4 实施记录（第八批）

交付闭环全部由宿主确定性代码执行并经执行层审计；真实模型评测与真实外部服务继续如实标注为未验收。

| 范围 | 实现与离线证据 |
| --- | --- |
| 受控 Git 入口 | [git_ops.py](../../pepsicode/orchestration/git_ops.py) 提取 M2 的净化 Git 调用（无用户配置/无 hooks/无终端提示、经 `execution.process.run`、感知取消与截止时间）为共享 `run_git`；[environment.py](../../pepsicode/orchestration/environment.py) 改为复用，行为不变（M2 回归通过）。共享 `.git` 的变更操作沿用 `own_workspace` 串行化。 |
| 环境清单与候选记录 | [candidate.py](../../pepsicode/orchestration/candidate.py) 对 `result_ready` 且有 prepared 环境的尝试记录候选：base SHA、分支、提交后的 head SHA、有界 diff 摘要（≤32 文件）。worktree 有未提交改动或无新提交时拒绝记录；head/分支变化把候选标记 `stale`（B17 候选侧）。 |
| 候选集成与冲突 | [integration.py](../../pepsicode/orchestration/integration.py) 从共同 base SHA 建立独立集成 worktree（`codex/integrate-<id>`），按序 cherry-pick（cherry-pick 需要提交者身份，冲突为确定性结果、立即 abort 并保留双方改动，B15；其余失败进入未知结果，不盲目重放）。回归覆盖双候选串行集成、同文件冲突（双方 worktree 与主工作区均未被污染）、未知结果后 `scope.has_unknown` 阻止新集成。 |
| 精确版本验收 | `run_integration_acceptance` 按 `verificationCommands` 在集成 worktree 内经执行层逐条运行验收命令，记录退出码/输出摘要/时长，绑定集成 head SHA、任务修订与命令摘要；运行前后 head 不一致则验收作废。候选或 checkout 后续变化把整条集成标记 `stale`、验收置无效——旧证据不通过新代码（B17）。未配置验收命令时不产生有效验收，交付被拒绝。 |
| 有限修复循环 | [repair.py](../../pepsicode/orchestration/repair.py) 失败分类（代码/环境可修复；权限/预算/传输/未知不自动修复）+ 失败指纹（含 summary 的稳定摘要，改日志不重置计数）；[store.retry_work](../../pepsicode/orchestration/store.py) 允许失败 `general` 尝试在 `orchestration.maxRepairAttempts`（[config.py](../../pepsicode/config.py)，默认 2）内按修复重试，continuation 携带失败证据。回归覆盖预算耗尽、同指纹拒绝（B13）与不可修复类别拒绝；explore 的既有自动重试不回退。 |
| 外部交付与幂等 | [external_delivery.py](../../pepsicode/orchestration/external_delivery.py) 交付已通过验收的集成：默认仅本地记录；`push` 前先 `ls-remote` 核对（已在远端 → 幂等 delivered，不重推），推送经执行层（审批/策略），非零退出为已知拒绝（failed），结果未知标记 `uncertain` 且永不自动重发，重推前必须 reconcile（B16）。`pull_request` 经 GitHub REST（令牌仅环境变量、`http_request_json` 可打桩）：同 head 已有 PR 幂等返回、未知响应 uncertain。交付记录持久化于 `orchestration.deliveries`。 |
| 协调工具与状态校验 | [coordinator.py](../../pepsicode/orchestration/coordinator.py) 新增 `task_integrate {work_ids}`（集成+验收一步完成）与 `task_deliver {integration_id, push?, pull_request?, repository?, base_branch?}`，沿用 `authorize` 守卫与 receipts 模式；[schema.py](../../pepsicode/orchestration/schema.py) 校验新增可选段 `candidates`/`integrations`/`deliveries`/`repairs`（旧会话无这些键时兼容，回归覆盖）。 |
| 保留策略 | 集成 worktree 不自动清理；`prune_integration` 仅接受已通过验收、冲突或已过期的集成并经执行层移除（B20）。回归 [test_m4_delivery.py](../../tests/core/test_m4_delivery.py) 覆盖：已验收集成清理后 worktree 与支持目录移除、验收证据保留在 pruned 记录中、validate 不再把 pruned 标记 stale；未验收（integrated）的集成拒绝清理且 worktree 保留。 |

验证记录（Windows 11, Python 3.12.4）：

- `python -m pytest -q tests/core/test_m4_delivery.py tests/core/test_m4_external_delivery.py`：16 项通过（真实临时 Git 仓库；推送用本地 bare 远端，PR 用打桩 HTTP 端点；含 B20 清理回归）。
- `python -m pytest -q tests/core/test_m2_environment.py tests/core/test_m2_process_channel.py tests/core/test_orchestration_store.py tests/core/test_task_orchestration.py tests/core/test_verification_resume.py`：79 项通过（M2/编排存储/验收/恢复回归未回退；`coordinator_turn` 工具集新增 task_integrate/task_deliver 的断言已同步更新）。
- `python -m ruff check pepsicode tests`、`python -m ruff format --check pepsicode tests`、`python scripts/check_project.py`、`python -m pepsicode.evals --mode smoke` 通过。
- 实机演示（已执行，经真实协调工具路径）：临时 Git 仓库 + 两个已提交候选 → `task_integrate` 一步完成候选记录、串行 cherry-pick 与验收（receipt：state=accepted、head 绑定、验收命令 exit 0）→ `task_deliver` 生成本地交付记录 → `task_deliver push` 经 reconcile→push→verify 交付本地 bare 远端 → 重复 `task_deliver push` 幂等（attempts 显示 `already_on_remote`，未重推）。未配置 `verificationCommands` 的会话 `task_integrate` 如实返回 acceptance_failed 并拒绝交付。
- 已知设计边界（与 M2 相同）：`prepare_environment`/M4 函数在各自会话副本上写编排状态，宿主在调度边界（`run_pending`）重载会话；协调轮内调用 M4 工具后，同一轮内其他经陈旧快照的保存可能覆盖新段——当前协调工具每次调用独立重读，宿主侧应在协调轮结束后重载再继续（与既有 M2 准备入口的约定一致，未新增跨轮测试）。

真实模型端到端评测（2026-11-14，用户授权小范围，`--mode real --track preset --runs 1`，报告 [.evals/20261114T010642629342Z/report.json](../../.evals/20261114T010642629342Z/report.json)）：

- 用例与结果：cross_file **通过**（76s，14 次请求，20,364 input / 12,155 output tokens）；resume **超时**（180s 墙钟截止，0 次模型请求——疑似模型连接停滞或环境性阻塞，非代码路径失败）；steering **失败**（7 次请求，7,473/1,269 tokens，花费约 $0.056——内部验收判定满足但隐藏检查 `value() == 43` 失败，属 false completion：模型自报完成不能替代外部评分，与清单"Worker 自报 ready 不等于完成"的边界一致）。
- 汇总：3 用例成功率 33.3%，21 次模型请求、27,837 input / 13,424 output tokens，0 基础设施错误，审批 2 次均即时处理。**样本量极小（n=3，单次运行），只说明评测管道可用与失败类型分布，不构成模型能力结论**；失败类型：连接停滞（1）、false completion（1）。
- 未覆盖：M4 交付链路（task_integrate/task_deliver）本身不在现有评测用例中——现有用例测单 Agent 编码任务；M4 机制的评测仍以脚本模型回归为准（见上表）。

未验收/剩余问题：

- 真实 GitHub 凭据下的推送、PR 创建与 CI 失败路径未实测（仅本地 bare 远端与打桩 HTTP 端点）；B11 宿主离线、B18 长任务压缩沿用既有上下文压缩机制与既有 compaction 评测用例，未针对 M4 交付链路专项扩展。
- 真实模型评测为小样本（n=3），未覆盖 M4 交付链路与更多工程场景；resume 用例的超时根因（0 次请求即超时）未定位。
- POSIX 上的交付链路未实测（沿用 M1/M3 POSIX 未验收记录）。

## 1. 最终修改目标

每个启用编排模式的会话有一个固定的 Orchestrator。它是用户直接对话的主 Agent，负责理解需求、派工、处理重要事件和汇总结果。Worker 接受有范围的子任务，独立运行。Python 内核负责可靠的队列、执行、状态、权限和事件投递。

完整交付时，编排会话归属于常驻服务；TUI、桌面和 IM 是可连接、可断开的控制端。实施类 WorkItem 拥有独立会话、worktree/分支、依赖环境和受管 Worker 进程，后台完成准备环境、实施、测试、有限修复和交付。操作系统安全隔离单独标明等级和验证证据。设计目标是减少用户过程性看护，在预授权范围内自动推进，并在明确问题、权限边界和交付时请求人类行动。

用户提交任务后应能观察到以下流程：

1. Orchestrator 将两个相互独立的调查交给 Worker A 和 B，派工成功即取得执行编号，无需等待调查结束。
2. A、B 在受管后台任务中运行，TUI 侧栏自动更新活动；主对话仍能接受问题、补充和停止指令。
3. A 提出必须回答的问题时，宿主将事件交给 Orchestrator；此时 B 可以仍在运行。
4. 用户回复后，系统准确关联问题、修订任务，并根据权限和修订检查决定哪些执行需要停止或重新派发。
5. A、B 返回结果时，Orchestrator 可继续分派有依据的后续工作，或进入父任务验收。
6. 最终是否完成由当前父任务的有效验收决定。排队成功、Worker 返回、Worker 自报 ready，都不等于用户任务完成。

**核心验收：某个 Worker 未结束时，Orchestrator 已能处理另一事件并产生下一步决策。仅把工具放入线程池，随后仍等待全部结果，不满足本次目标。**

**完整目标还必须验证：控制端断开后任务继续、两个 Worker 在各自工作区独立修改并测试、单 Worker 进程崩溃可局部收敛、最终交付绑定准确的代码版本和验收证据。仅完成异步聊天和状态侧栏不算完整交付。**

## 2. 当前基础与参考来源

### 2.1 PepsiCode 已有基础

| 已有能力 | 本次处理原则 |
| --- | --- |
| `TraceManager`、宿主活动事件、统一状态投影 | 继续复用，不另建前端推测的状态系统 |
| `agent_report`、`agent_status`、问题记录 | 扩展事件交付与归属，保留现有报告语义 |
| TUI 自动侧栏、桌面 Agent 卡片、QQ 状态查询 | 接入异步状态，不要求用户反复查询 |
| `task` 同步调用和只读批次并发 | 保留兼容路径；异步模式增加明确的派工接口 |
| `TurnInputQueue`、任务修订、会话树和原子保存 | 扩展为长期任务生命周期内的输入与事件处理 |
| `ExecutionScope`、预算、取消、审批、恢复日志 | 延长正确的所有者生命周期，不绕过既有执行入口 |
| 剩余轮数提醒、达到上限时保留报告 | 保留；进一步核对异步执行中的共享预算和总结机会 |

### 2.2 必须参考的 dsh 实现

参考仓库为 `E:/Code/dsh/dsh-superagent-prototype-main`。下列路径相对于该仓库。

| 参考位置 | 借鉴的实际机制 |
| --- | --- |
| `prompts/orchestrator.md` | 固定协调角色，Worker 调查和实现；无事时结束本轮，等待消息驱动 |
| `src/app/runtime.ts` | 持久的主会话身份、统一运行时装配 |
| `src/executor/dsh.ts` | 每工单独立 Worker 会话、异步启动、宿主事件监听、WorkerSlots 并发控制 |
| `src/app/effects.ts` | 状态与待执行动作同事务提交，提交后派发；顺序、合并、退避和故障可见 |
| `src/app/sessions.ts`、`src/app/delivery.ts` | 区分触发协调的消息与仅注入的通知，按稳定消息 ID 去重 |
| `src/app/tools/main.ts` | 路由、查询、补充信息、请求状态、受约束的重试 |
| `src/app/tools/executor.ts` | Worker 结构化报告与问题，同步记录后可靠转交 |
| `src/core/stage.ts` | 从持久事实计算展示阶段，不让模型直接写任意 UI 状态 |

借鉴这些职责和交付保证，不照搬 dsh 的 GitHub/飞书业务、默认权限或数据库实现。该参考项目本身是研究原型，不能把论文设想当成已实现能力。

### 2.3 对参考项目的事实边界

以下依据本机参考仓库核对，作为设计判断的边界：

- `docs/003-execution.md`：每张 Ticket 有独立 Executor Session 和目录；准备脚本可以创建 Git worktree。独立分支依赖准备配置，不是所有部署都自动保证。
- `docs/006-deployment.md`：两类 Agent 以操作者系统用户的 `danger-full-access` 运行，Worker 可以读取用户目录；命令包装器不隔离进程。独立目录、Git 分支、独立会话都不能自动证明操作系统安全隔离。
- `scripts/dsh-superagent`：提供基于 tmux 的本地服务启动、停止、健康检查等管理。关闭连接终端与关闭执行机器是不同事件；执行机器关机后任务无法继续，远程常驻主机必须保持运行。
- `docs/000-philosophy.md` 和 `docs/003-execution.md`：减少打扰，但阻塞、失败、待答问题和等待审查仍需要人类介入，并非所有情况都只在最后批准一次。
- `README.md`：公开代码明确是研究原型，并非生产就绪；不能据此承诺任意崩溃都不影响系统、任意冲突都自动合并、任意副作用都可回滚。

不以“某类框架必然共用上下文”或“Token 必然指数级增长”为设计前提。独立上下文、请求量、故障影响范围和隔离能力都必须以当前实现及测量证明；PepsiCode 已有子 Agent 独立上下文，本次要补齐的是其运行、资源和交付生命周期。

## 3. 能力范围与阶段边界

| 里程碑 | 必须交付 | 验收含义 |
| --- | --- | --- |
| M1 异步协调基础 | 固定 Orchestrator、持久派工、事件驱动、三端状态和可靠恢复 | 原清单第一至第五批，只证明进程内异步编排 |
| M2 独立执行环境 | 每项实施任务独立 worktree/分支和依赖环境、Worker 子进程、预算代理、资源限制、隔离等级 | 见第 18 节；不能把线程或目录称作安全沙箱 |
| M3 常驻低打扰服务 | 服务与客户端分离、启动/停止/健康检查、预授权运行、GitHub/CI 事件与既有 QQ 通道接入 | 见第 19 节；关闭客户端后继续，执行宿主需在线 |
| M4 工程交付闭环 | 环境准备、修改、测试、有限修复、候选集成、PR 交付和版本化验收 | 见第 20 节；不要求自动合并主分支或无限自愈 |

以下表格区分 M1 的过渡实现和完整目标。第 18–21 节的要求也是正式交付要求，不能作为可忽略的设想。

| 维度 | M1 过渡实现 | 完整目标及保留边界 |
| --- | --- | --- |
| Orchestrator | 每会话一个稳定协调身份；同一时刻最多一轮决策 | M3 服务持有稳定身份；不采用所有用户共用一个上下文或多个协调者竞争同一会话 |
| Worker | 异步执行、独立上下文、可查询、可停止、结果可追溯 | M2 独立进程与明确隔离等级；不允许自行扩大任务或无限生成下级 Worker |
| 调度 | 持久待执行队列、并发名额、重要事件唤醒、有限重试 | M3 接收外部事件；本次不做通用 DAG 引擎、智能抢占或跨机器分布式调度 |
| 工作区 | 多个纯只读任务并行；共享工作区排他写入 | M2 独立 worktree 并行修改，M4 在独立集成区处理结果；同一工作目录仍不允许多写者 |
| 运行范围 | 同一宿主进程内后台运行，主 Agent 的一轮结束不终止 Worker | M3 常驻服务与客户端解耦；不承诺执行宿主关机后继续或无人值守永久续行 |
| 恢复 | 恢复队列、事件、结果和预算；显式继续后恢复获准工作 | M3 可按有效预授权恢复已接受任务；未知副作用仍禁止自动重放 |
| 通道 | Python 内核统一实现；TUI、桌面、QQ 消费同一协议 | M3 连接统一服务；不为每个通道复制一个调度器 |
| 自动处置 | 对确定可重试的内部投递故障做有限重试 | M4 在已授权范围和预算内修复测试失败；不自动忽略审批、无限重试或增加预算 |
| 验收 | 复用父任务修订、验收条件和当前证据 | M4 绑定候选 head 和实际交付；不能用 Worker 自报完成、测试数量或百分比代替验收 |

首版以会话显式开启 `orchestration.enabled` 为入口，默认关闭以保留现有调用方语义。启用后不能在同一父任务中混用两个调度所有者。兼容完成并有验证证据后，再另行评估是否默认启用。

M1 的只读并行与写入串行采用保守边界：同一真实工作区内，执行可能修改文件、启动进程或调用外部修改服务的 Worker 持有排他执行名额；持有期间不启动依赖实时文件内容的其他 Worker。M2 将实施任务分配到独立 worktree 后可跨工作区并行，Git 公共元数据、共享缓存和外部服务仍单独管理冲突，不能仅因目录不同就认为所有资源已隔离。

## 4. 角色和权限要求

### 4.1 Orchestrator

- [ ] 将当前主对话明确配置为 Orchestrator 角色，不额外创建一个同时抢占用户对话的“监控模型”。协调身份跨普通续轮和会话恢复保持稳定。
- [ ] 对简单解释和汇总可直接回答；需要实施的工作通过受限派工工具交给 Worker，避免为简单回答强行生成子任务。
- [ ] 提供任务分解、派工、状态查询、补充上下文、问题转交、停止子任务、申请重试及请求父任务验收的能力。
- [ ] 工具白名单由宿主执行层约束。可读受限项目上下文和任务状态；不直接拥有编辑文件、执行命令或外部修改工具，也不能通过动态工具加载重新取得这些能力。
- [ ] 父任务验收由宿主调用现有验证入口；所需命令仍经过执行审批，不能借“验收”绕过 Orchestrator 的工具限制。
- [ ] 禁止用 sleep、轮询工具或阻塞等待工具占住协调轮次。无可处理事件时提交本轮结果并回到等待状态。
- [ ] 状态摘要、报告和外部输入属于数据，不得被提升为新的用户授权。Worker 无权通过报告修改总预算、验收要求或权限模式。

### 4.2 Worker

- [ ] 每个派工记录明确目标、范围、工作区、角色、允许工具、父任务及启动修订、期望交付物和预算。
- [ ] 每次执行有独立 `trace_id`、执行范围、消息历史和取消信号；重试生成新执行记录，保留旧记录。
- [ ] 复用 `agent_report` 的 progress、ready、blocked、failed、dropped 及问题语义。宿主绑定身份与时间，模型不能伪造归属。
- [ ] Worker 只能写自己的报告和输出，不能直接写主会话消息、父任务验收结果或其他 Worker 的状态。
- [ ] 首版 Worker 不暴露派工工具；确需进一步拆分时，通过报告请求 Orchestrator 决策。
- [ ] 必答问题令当前执行安全收尾，保存进展后释放可释放资源；回答后创建新的执行尝试。可选问题允许继续独立检查。
- [ ] Worker 的上下文保留可供恢复的摘要与历史引用。不得仅保存一句最终报告，随后靠重做整个任务恢复上下文。

### 4.3 宿主调度器

- [ ] 调度器是确定性 Python 代码，负责入队、状态转换、租用执行名额、取消、事件路由和恢复，不用模型做状态轮询。
- [ ] 同一会话仅有一个状态写入所有者。多线程通过命令和事件提交变更，不各自覆盖整个 `SessionData`。
- [ ] 增加宿主所有者身份和运行代次。第二个进程尝试同时控制同一会话时必须明确拒绝或只读连接，不能同时派发同一队列。
- [ ] 同一工作区的排他写入限制必须覆盖该宿主内的不同会话；跨进程无法保证时拒绝取得第二个写入所有权，不宣称已有操作系统沙箱隔离。

## 5. 生命周期和数据模型

不能把“主 Agent 当前一轮”“父用户任务”“一次 Worker 执行”当成同一个生命周期。

| 对象 | 含义与建议的最少字段 |
| --- | --- |
| OrchestrationRun | 父任务的编排运行；`run_id`、`session_id`、分支、`task_id`、当前修订、宿主代次、运行模式、授权策略版本/有效期、状态、预算引用、时间与截止时间 |
| WorkItem | 一件逻辑子任务；`work_id`、`work_key`、父任务、目标、范围、角色、读写类别、当前尝试、排队顺序、结果引用 |
| Attempt | 一次实际执行；`attempt_id`、`trace_id`、`work_id`、启动修订、执行范围、上下文引用、workspace/environment 引用、进程身份/代次、启动/结束时间、退出原因、未知副作用引用 |
| Workspace / Environment | M2 工作环境；仓库身份、base SHA、分支、worktree 路径、环境/锁文件摘要、隔离等级、共享资源声明、准备阶段和产物引用 |
| Event | 一条事实或报告；`event_id`、持久序号、来源身份、运行/任务/尝试归属、任务修订、类别、载荷引用、产生时间 |
| Effect | 提交后待执行的动作；`effect_id`、种类、幂等键、目标、前置状态/代次、顺序、载荷版本、领取状态、次数、下次尝试时间、最后错误 |
| Delivery | 事件进入协调上下文的凭据；事件 ID、目标协调身份、待投递/已领取/已入历史状态、消息 ID、消费轮次引用 |

- [ ] 数据格式带版本，新增字段有兼容默认值；旧会话在未启用编排时仍走原流程。
- [ ] `work_id` 标识逻辑工作，`trace_id` 标识一次执行；不得用 Agent 名称当身份。
- [ ] `ExecutionScope.task_id` 与用户任务 ID 分别保存，禁止复用同一个字段表达不同身份。
- [ ] `dispatch_key`/`work_key` 在父任务范围内唯一。重复派工键与同一输入摘要返回已有记录；键相同但目标或权限范围不同应返回冲突，不能静默覆盖或再启动一份。
- [ ] 一条 WorkItem 同时最多有一个有效执行尝试。终态保持历史不可覆盖，重试通过新增 Attempt 表达。
- [ ] 大输出和完整历史使用分页/现有 artifact 引用。模型请求只携带有界摘要，UI 从快照读取详情。

建议保留三层状态，实际字段名可以调整但语义必须一致：

1. 编排运行：active、waiting_user、paused、completed、cancelled、blocked、recovery_pending。
2. WorkItem：queued、active、waiting_user、result_ready、blocked、failed、cancelled、stale。
3. Attempt：复用 TraceManager 的宿主执行状态；排队阶段由 WorkItem 表达，不伪造已经开始的工具活动。

`result_ready` 表示已有待父任务核验的结果。Orchestrator 轮次 idle 不会将编排运行标记 completed；完成判定还必须考虑待处理事件、活跃执行、未知副作用和父任务验收。

## 6. 异步派工接口契约

建议新增下列模型工具名，也允许在设计评审中改名。接口语义和兼容要求必须保持。

| 工具 | 要求 |
| --- | --- |
| `task_dispatch` | 校验范围与预算后，原子写入 WorkItem、Attempt 标识及启动 Effect；返回 accepted/queued 和执行身份，不等待 Worker 输出 |
| `agent_status` | 延续只读快照查询，能查询排队、当前执行、历史尝试、结果及问题，不唤醒 Worker |
| `task_note` | 向目标工作交付有界上下文；不能替代用户指令修改授权。影响需求时必须进入已有任务修订流程 |
| `task_cancel` | 发出幂等停止请求，返回 stop_requested；直到真实执行与所需后台工作收敛才宣告停止 |
| `task_retry` | 检查可重试原因、授权、旧尝试终态、未知副作用与预算，创建新 Attempt；不能只把状态改回 running |
| `agent_report` | 保持 Worker 上报接口，将需要协调的报告可靠写入事件队列 |

派工响应示意，字段名称最终须与协议一同确定：

```json
{
  "accepted": true,
  "run_id": "run-...",
  "work_id": "work-...",
  "attempt_id": "attempt-...",
  "trace_id": "trace-...",
  "state": "queued",
  "task_revision": 3
}
```

- [x] `accepted` 只表示派工持久化成功。写盘失败时不得返回 accepted，也不得先启动 Worker。见实施记录 A04、A05。
- [ ] 工具重放、重复网络请求和重复事件不能生成第二个有效执行；需要使用已持久化的幂等身份，而不是每次随机生成新的请求 ID。
- [x] Orchestrator 的异步模式不暴露旧同步 `task` 作为绕行路径；旧模式的调用方继续获得原来的完成结果格式。见[交互工具装配](../../pepsicode/orchestration/interactive.py)和[协调轮次回归](../../tests/core/test_orchestration_store.py)。
- [ ] 首版不提供让 Orchestrator 阻塞等待全部 Worker 的工具。等待由事件驱动的下一轮协调完成。
- [ ] 明确区分“请求 Worker 补充报告”与“读取已有状态”；前者可能消耗模型预算，只在有依据且获准继续时触发。

## 7. 事件分类和唤醒规则

| 事件 | 更新 UI/记录 | 触发 Orchestrator |
| --- | --- | --- |
| 启动、工具活动、耗时、token 更新 | 是，可合并高频更新 | 否 |
| 普通 progress 报告 | 是，保留最近摘要 | 默认否 |
| ready/失败/阻塞/放弃/执行超限 | 是，保留成果与原因 | 是，按尝试和版本去重 |
| 必答问题 | 是，保留问题 ID 和提问者 | 是 |
| 可选问题 | 是 | 合并转交，不强制暂停整个父任务 |
| 用户新输入、补充或回答 | 是 | 是，沿用输入模式和任务修订规则 |
| 审批请求 | 是 | 先交由现有审批通道处理，模型不能代替用户批准 |
| 停止/取消 | 是 | 宿主立即处理，不等待模型作出决定 |
| 新的未知副作用、持久化失败 | 是 | 进入明确阻塞或恢复状态，不盲目续行 |
| 已结束/旧修订执行的迟到事件 | 历史留档 | 不触发当前任务的新执行 |

- [ ] 事件生产者先提交状态和待投递记录，再通知订阅者；没有 UI 订阅者也必须保留关键事件。
- [ ] 同一协调会话最多一个模型轮次运行。协调者忙时事件进队列，下一安全边界合并消费，禁止同时写两条主对话历史。
- [ ] `ready` 报告和随后宿主结束事件可以合并成一次协调输入；新产生的不同问题不能被合并丢失。
- [ ] 每批协调输入有条数、字数和最大等待时间限制。持续到来的进展不能让结果/问题事件永久等待。
- [ ] 取消、权限收紧和用户补充优先于普通结果；调度公平性应防止一直到来的新任务饿死已排队工作。
- [ ] 主 Agent 的回答和它自己提交的派工命令不能反复触发自身唤醒。
- [ ] 无事件、无可执行动作时不产生模型请求；内部用于截止时间和有限重试的定时器不能演变成模型轮询。

## 8. 宿主调度与并发要求

- [ ] 增加会话级受管 Runtime。其生命周期覆盖父任务的多次协调轮次和 Worker 执行，不依附于单次 `run_agent_turn` 的栈帧。
- [ ] M1 调度实现可复用现有线程池/异步宿主，但不能把同步 `task` 包一层 `create_task` 后就当作完成。M2 Worker 进程模型按第 18 节实现，两阶段都必须有持久化队列和可追踪的所有者。
- [ ] 创建执行上下文时显式绑定父运行、会话、预算、取消、deadline、权限和任务修订读取器。不得捕获随后退出的临时线程局部上下文或已关闭的资源。
- [ ] 核对 `scoped_turn` 的结束行为：协调者一轮返回不能结算整个父运行、销毁预算对象或取消 Worker 的截止时间保护。
- [ ] 记录排队、启动、运行、等待与停止的区别。启动预约也占用并发名额，防止同时通过容量检查后超卖。
- [ ] 同一工作区执行纯只读 Worker 可并行；写入类 Worker、修改命令、父任务验收命令通过统一排他入口串行调度。M2 独立工作区之间允许并行，不取消共享 Git 元数据和最终集成的串行约束。
- [ ] 权限审批等待如果仍占着实际执行线程，必须计入执行容量，不能仅修改显示状态就释放名额并超卖。
- [ ] 必答问题已令 Worker 真正结束时可释放执行名额。仍需其结果的后台进程继续计入活跃资源，不能根据一条 ready 报告提前释放。
- [ ] 用户控制和 Orchestrator 决策不被 Worker 名额耗尽锁死；它们仍受同一父任务总预算约束。
- [ ] UI 回调、状态发送和模型请求均不在会话锁内等待。规定会话、调度队列、追踪和预算锁的顺序并测试死锁场景。
- [ ] 所有后台线程/任务均登记并受取消管理；不能使用无人持有的后台任务或靠 daemon 线程承担交付保证。

## 9. 持久化、去重和恢复要求

首版优先复用现有会话存储和原子提交入口。可将编排状态与待执行队列放入同一版本化会话快照；如改用 SQLite，必须先提交存储迁移设计，指定唯一权威来源与跨存储提交顺序。不能同时维护两份互相独立的真实状态。

- [ ] 业务状态变更与相应 Effect 同一原子提交；真正启动 Worker、投递消息和发送通知发生在提交之后。
- [ ] Effect 具有 pending、claimed、settled/failed 等领取状态与运行代次。旧执行者迟到返回不能删除更新后的待执行动作。
- [ ] 不仅保存事件队列，还保存事件进入协调消息历史的稳定 ID。先确认消息历史提交成功，再确认交付完成。
- [ ] 崩溃发生在“领取后、消息提交前”时可重新投递；发生在“消息提交后、确认前”时通过消息 ID 去重，不重复派工。
- [ ] 不宣称外部副作用 exactly-once。启动或调用结果无法证明时进入恢复核对，禁止通过重新执行猜测第一次是否成功。
- [ ] 关键写盘失败暂停新的派工或事件确认，并向用户显示错误；不能只依赖内存继续运行。已启动工作按现有停止/未知结果规则收敛。
- [ ] 高频 UI 活动可以合并；结果、必答问题、取消、修订和未知副作用不得因队列溢出静默丢弃。队列满时反压或显式拒绝新的派工。
- [ ] 恢复时已结束结果保留；无法证明仍存活的执行标为 interrupted/recovery_pending；旧进程所有者不得再次取得新执行权限。进程隔离模式不得仅凭同一个 PID 判断存活，还需校验启动身份或受管 IPC 握手。
- [ ] 打开/浏览历史会话只恢复视图，不自动发模型请求。M1 在用户明确继续后恢复获准的排队动作；M3 服务恢复可按第 19 节的有效预授权推进已接受任务，不能把浏览操作当成续行授权。
- [ ] 已经启动过的写入、进程和外部服务调用恢复时必须先依据执行日志核对；重试不能代替 reconcile。
- [ ] 会话 fork/clone 不复制活跃运行所有权、队列消费权和可执行 Effect。切分支不能激活旧分支遗留任务。

## 10. 任务修订、取消和验收边界

- [ ] 明确的状态查询入口只读取事实，不增加任务修订、不取消 Worker。普通用户输入按已声明的 steer/follow_up 语义处理，不能由模型随意把修改需求降格成“查询”来绕过修订检查。
- [ ] `steer`、问题回复及其他需求变化沿用 `task_control` 的修订机制；只把文本塞给 Worker 而不更新修订是不合格实现。
- [x] 新修订生效后，旧修订的未启动派工立即失效；已运行 Worker 在下一执行入口被检查和停止。已发生的副作用不会自动回滚。见实施记录 A12。
- [ ] 当前版本不能直接继承旧版本 ready 或验收证据。需要复用历史发现时以带来源的上下文传递，是否仍有效必须重新确认。
- [ ] 用户停止整个父任务时，立即阻止新派工与后续协调唤醒，取消相关 Worker、排队动作及受管后台工作；保留历史结果。
- [x] 单独停止 Worker 只设置其局部取消信号，不意外取消兄弟执行。替换 Worker 前确认旧尝试已经终止或进入不可重放的恢复状态。见实施记录 A11、A13。
- [ ] 仅在宿主已收敛或明确报告仍有未结束资源后返回相应结果；不能把停止请求已接受显示成“全部已停止”。
- [ ] 子任务尚未完成、仍有必答问题、未知副作用或未处理的关键事件时，不允许把整个父任务报告为完成。
- [ ] 父任务验收须匹配当前修订、验收轮次和工作区证据，验证期间控制工作区并发写入；沿用现有证据过期规则。
- [ ] “所有 Worker 都结束”只触发汇总/验收机会；失败、超限、被取消和部分结果必须在最终汇总中明确说明。

运行模式必须显式区分。M1 的 interactive 模式保留控制端关闭时停止它所拥有运行的策略；M3 的 service 模式由常驻服务拥有运行，客户端断开只解除订阅，不停止已授权任务。显式“停止任务”“停止服务”仍必须执行。UI 显示当前模式及退出后是否继续，重连不改变权限；不能对两种模式使用同一条断连取消逻辑。

## 11. 预算和收尾要求

- [ ] 父运行持有共享预算，Orchestrator、Worker、重试、摘要和验收模型请求都计入；每次事件唤醒不能新建一个满额预算。
- [ ] 实际提供者入口继续记录 `model/prepared` 和使用量，新增角色指令、轮数提醒与事件摘要必须在实际请求记录中可重建；不记录认证头。
- [ ] 恢复会话不能重置已用请求数、已知费用或费用不确定标记。调整预算只有经过用户明确授权才生效，并保留变更记录。
- [x] 给 Worker 的可用请求配额不得耗尽父运行预留的协调收尾额度。预留从已有总预算中划分，不是增加额度。见[请求预算回归](../../tests/execution/test_request_budget.py)。
- [ ] Worker 每轮明确获知剩余轮数，临近上限时要求交付已知成果、证据和未完成事项；模型忽略提醒时仍按实际上限停止。
- [ ] 轮数、实际模型请求次数和工具调用次数分别计数与展示，不能把三者混为一个数。
- [ ] 达到上限显示“达到执行上限”或更具体的“达到轮数上限”，保留最后有效报告和退出原因，不笼统归为代码执行失败。
- [ ] 收尾不额外突破请求、费用或截止时间预算。无法再请求模型时交付已保存的结构化报告，明确这是部分成果。
- [ ] 配置费用上限时，若沿用当前串行核算实现，应如实说明模型传输可能串行；不能为并发吞吐绕过金额护栏。
- [ ] 无新证据的同类失败、同一工作反复派发、事件唤醒无进展必须有上限；达到上限停在明确可解释的状态，等待用户处理。

建议首版配置及默认值如下。字段名可以调整，默认值必须显式配置、校验并写文档。

| 配置 | 建议默认值 | 约束 |
| --- | --- | --- |
| `orchestration.enabled` | false | 会话内明确开启；运行期间不能混用新旧所有者 |
| `maxConcurrentWorkers` | 3 | 正整数；写入仍受工作区排他约束 |
| `maxQueuedWorkItems` | 32 | 达到上限拒绝新的派工，返回原因 |
| `maxActiveWorkItemsPerRun` | 32 | 包含排队、执行、等待用户及待验收，避免等待项绕过容量限制 |
| `maxAutoRetries` | 1 | 仅适用于明确可重试的瞬时故障，且仍消耗总预算 |
| `maxCoordinatorActivations` | 20 | 当前父运行累计；达到后停止自动唤醒，用户控制仍可处理 |
| `eventBatchLimit` | 8 | 每批最多事件数，保留剩余游标 |
| `eventContextChars` | 12000 | 事件上下文总字数上限；完整内容通过引用读取 |
| `eventDebounceMs` / `eventMaxDelayMs` | 250 / 1000 | 合并普通结果，关键控制指令不延后到模型消费 |
| `coordinatorReserveRequests` | 2 | 从父运行已有配额中保留；总额不足时拒绝派工或直接汇总 |
| `executionMode` | interactive | service 模式需完成 M3 并有明确授权；客户端不可偷偷切换 |
| `workspaceMode` | shared_serial（仅 M1） | M2 实施任务使用 isolated_worktree；禁止悄悄退回共享目录 |
| `workerIsolation` | process（M2） | hardened 模式须有经过验证的 OS 隔离后端；显示实际生效等级 |
| `maxRepairAttempts` | 2 | M4 测试失败修复上限，仍受父运行总预算和无进展限制 |
| `unattendedPolicy` | 未配置 | 未配置或过期时不得开启无人看护执行；授权范围见第 19 节 |

这些默认值是首版产品约束建议，不是对真实性能的承诺。模型能力、并发配置、次数和时间限制均应由接口或配置表达，不按模型名字猜测。

## 12. TUI、桌面与 QQ 的用户体验及协议要求

- [ ] 保留自动 Agent 侧栏，增加排队、正在启动、等待回复、停止中、结果待核验和达到上限等有依据的展示。
- [ ] 主对话显示 Orchestrator 正在协调或等待事件；Worker 运行中不锁死用户输入，也不把输入误发给任意一个 Worker。
- [ ] 必答问题显示提问 Worker、任务摘要和问题 ID；回复准确关联。多个问题同时存在时不猜测用户回答的是哪个。
- [ ] 继续支持 `/agents` 和 `/answer`。排队/停止/重试入口应同时覆盖 TUI、桌面和 QQ，通道差异只影响表现形式。
- [ ] 增加运行快照，明确父运行状态、Orchestrator 是否忙、Worker 数量、队列长度、待答问题和停止能力。
- [ ] 区分“协调者本轮结束”“编排运行仍在工作”“父任务完成”。不得复用一次 `turn/end` 把客户端状态清空或误报完成。
- [ ] 为异步模式定义协议能力协商/版本标识。旧客户端不支持时显式走同步兼容模式或拒绝开启，不静默更改 `turn/run` 返回含义。
- [ ] TUI `is_busy`、桌面 `isRunning`、QQ 暂停与队列状态不能继续仅依据一个主模型线程。运行中切项目、切会话、重生成和退出的可用性必须逐项定义。
- [ ] 首版运行未结束前禁止迁移其工作区或会话分支；用户仍可查看历史与状态，跨会话只读查询不改变执行所有权。
- [ ] 重连先取得带版本/序号的快照，再消费增量。重复、乱序、旧会话事件不得覆盖新状态；慢消费者不阻塞提交或 Worker。
- [ ] 侧栏的短摘要、完整执行详情、最终汇总分层展示，去掉重复错误，保留成果。严禁用无依据的百分比假装完成进度。
- [ ] 默认只读模式、Plan 模式和审批弹窗行为在所有通道一致；协调者和后台任务均不能代用户点击批准。
- [ ] M3 UI 必须区分“断开查看”和“停止任务”，显示服务存活、上次同步、待处理通知和实际隔离等级；关闭 TUI/桌面不得隐式退出整个常驻服务。

## 13. 建议改造位置

下列“建议新增”路径尚不存在，开发人员可在保持职责清晰的前提下调整。不要把全部调度逻辑继续堆入 TUI 或主循环。

| 位置 | 改造责任 |
| --- | --- |
| `pepsicode/orchestration/`（建议新增） | 运行所有者、WorkItem/Attempt/Event/Effect 模型、调度、事件收件箱和恢复 |
| `pepsicode/agents/builtins/orchestrator.md`（建议新增） | 明确协调角色、决策范围、事件处理规则和交付要求 |
| `pepsicode/agents/loader.py`、`tool_filter.py` | 加载角色、限制工具集合，阻止动态加载绕行 |
| `pepsicode/tools/task.py`、新增派工/控制工具 | 拆分派工与执行；保留同步兼容层；执行实现复用现有工具入口 |
| `pepsicode/agents/trace.py`、`status.py` | 逻辑工作与尝试关联、排队/取消/历史投影，兼容旧记录 |
| `pepsicode/tools/agent_report.py`、`agent_status.py`、`agents/notices.py` | 持久事件生产、幂等交付、查询与有界模型摘要 |
| `pepsicode/core/agent_loop.py`、`sub_agents.py` | 协调轮次边界、上下文构造、正确绑定执行范围，不负责整个调度队列 |
| `pepsicode/core/session.py`、`session_tree.py`、`input_queue.py` | 原子保存、单写入所有者、恢复、消息去重、长期输入接收 |
| `pepsicode/core/task_control.py`、`task_verification.py`、`verification.py` | 修订失效、问题关联、完成条件和验收边界 |
| `pepsicode/execution/runtime.py`、`process.py` | 长期父运行、共享预算、取消/deadline、未知副作用与资源收敛 |
| `pepsicode/server/app.py`、`protocol.py` | 运行与连接所有权、异步协议、能力协商、快照/事件订阅 |
| `pepsicode/tui/app.py`、`agent_panel.py`、CLI 命令 | 自动状态、输入和控制交互，退出语义 |
| `pepsicode-client/src/stores/session.ts`、对话/轨迹组件 | 独立运行状态、消息生命周期、问题和停止入口 |
| `integrations/qq/src/bridge.js`、`store.js` | 新状态消费、准确回复、去重及连接恢复 |
| `pepsicode/worktree/` | M2 复用并改造 worktree 准备、身份校验、保留/清理；禁止默认复制凭据或共享可写依赖 |
| `pepsicode/sandbox/`、`execution/` | M2 Worker 进程、资源限制、IPC 权限与预算代理、可验证的 OS 沙箱适配 |
| 常驻服务与事件源模块（建议新增） | M3 服务宿主、生命周期、授权策略、GitHub/CI 事件收件箱和低噪声通知发件箱 |
| 环境准备与交付模块（建议新增） | M4 环境清单、测试修复循环、候选集成、PR 交付凭据及保留策略 |

## 14. 分批实施与交付要求

第一至第五批完成 M1；第六、七、八批分别完成 M2、M3、M4。每一阶段单独验收，报告只能声称已达到的阶段。

### 第一批：协议、所有权和数据契约

- [ ] 提交设计说明，冻结本清单中的状态机、运行生命周期、单写入所有权、锁顺序和兼容方案。
- [ ] 明确配置范围、协议版本、存储版本、所有幂等键及旧会话迁移。
- [ ] 建立可控的脚本 Worker、模型与故障注入夹具，先实现验收矩阵中的失败场景。

交付证据：设计文档、数据结构、迁移样例及失败测试。此批不能宣称异步调度已经完成。

### 第二批：持久派工与受管执行

- [ ] 完成 `task_dispatch` 的校验、原子入队、立即返回和提交后启动。
- [ ] 完成并发名额、工作区排他、单逻辑工作单尝试和执行资源登记。
- [ ] 保证父协调轮次结束后 Worker 仍受相同预算、取消与截止时间控制。

交付证据：阻塞 Worker 的情况下派工已返回，主会话可接受输入；重复派工和保存失败测试通过。

### 第三批：固定 Orchestrator 与事件闭环

- [ ] 配置固定角色和受限工具，完成重要事件唤醒、忙时合并、消息去重与无事件休眠。
- [ ] 打通“Worker A 提问，B 仍运行，Orchestrator 已处理问题”的路径。
- [ ] 打通“一个结果到达后，协调者根据结果派出后续任务”的路径。

交付证据：按事件 ID、运行/尝试 ID 关联的时间线，以及模型请求记录；不能只提交最终聊天文本。

### 第四批：停止、修订、恢复和验收

- [ ] 实现全局/局部取消、旧版本失效、权限收紧、队列反压和预算结算。
- [ ] 完成各提交边界的崩溃恢复、未知副作用核对、显式继续和旧会话兼容。
- [ ] 证明 Worker 返回不等于父任务完成，验收证据未被弱化。

交付证据：恢复与执行边界回归、磁盘故障注入、预算恢复和当前修订验收测试。

### 第五批：三端交互与文档

- [ ] 完成 TUI、桌面、QQ 的运行状态、问题、取消/重试入口以及异步协议适配。
- [ ] 验证旧客户端、慢订阅者、重连、切会话和关闭宿主的实际行为。
- [ ] 更新用户指南、架构文档与配置说明，交付可复现演示步骤和已知限制。

交付证据：自动化交互回归、真实界面演示、版本兼容说明；涉及发布/推送时另按项目授权流程处理。

### 第六批：M2 独立执行环境

- [ ] 完成第 18 节的 worktree、分支、依赖环境、Worker 进程、预算代理、资源限制和隔离等级。
- [x] 在两个独立工作区对同名文件并行修改和测试，证明主工作区、另一个 Worker 与宿主状态未被污染。见 2026-11-14 M2 实施记录（B01）。
- [x] 验证杀死一个 Worker 进程后其他 Worker 与调度服务仍可继续；保留被杀执行的结果、日志及未知副作用。见 2026-11-14 M2 实施记录（B03）。

交付证据：进程与工作区身份记录、隔离测试、资源限制测试，以及 hardened 模式实际支持的平台清单。未通过安全隔离测试时只能声称进程/工作区隔离。

### 第七批：M3 常驻服务与低打扰协作

- [ ] 完成第 19 节的独立服务宿主、客户端 attach/detach、健康检查、停止语义、预授权策略与重启恢复。
- [ ] 接入 GitHub/CI 事件及现有 QQ 控制链路；其他 IM 适配按独立交付范围实施，不默认扩大到全部平台。
- [ ] 证明关闭客户端后已授权任务仍可推进，重新连接可读到结果；用户停止服务后外部守护器不擅自拉起已停任务。

交付证据：无人看护演示、事件与通知去重记录、授权撤销测试、服务恢复及宿主离线说明。

### 第八批：M4 工程交付闭环

- [ ] 完成第 20 节的环境准备、测试修复、候选集成、精确版本验收和受授权 PR 交付。
- [ ] 覆盖依赖安装失败、测试失败、合并冲突、CI 失败、基线变化和外部投递结果未知等实际工程路径。
- [ ] 提交端到端软件工程评测与资源/上下文用量报告，明确成功率、失败类型和未支持场景。

交付证据：从任务到代码版本、测试证据、PR/本地产物的完整关联；不能用“多个 Agent 最终都说完成”作为闭环证明。

## 15. 必须通过的验收场景

| 编号 | 场景 | 通过标准 |
| --- | --- | --- |
| A01 | 两个 Worker 保持阻塞 | 两次派工均已返回身份；协调轮次已结束；Worker 仍受管运行；第三个用户输入可被接收 |
| A02 | A 提问，B 未完成 | Orchestrator 在 B 结束之前处理 A 的问题；不能等同批全部 join |
| A03 | A 返回结果，B 未完成 | 事件允许协调者产生下一步决策；后续派工按容量排队，不失去 B 的执行归属 |
| A04 | 同一派工请求重复十次 | 只有一个逻辑工作和有效尝试，返回同一身份；冲突参数被拒绝 |
| A05 | 派工原子保存失败 | Worker 未启动，调用方明确收到失败，不能声称任务已接受 |
| A06 | 领取后崩溃、提交后确认前崩溃 | 分别可重新投递或按消息 ID 去重；同一结果不会触发重复有效派工 |
| A07 | 启动结果或外部副作用未知 | 进入恢复核对，自动启动/重放计数为零 |
| A08 | 并发上限与等待审批 | 实际活跃/启动数量不超配置；审批仍能响应；无死锁或名额泄漏 |
| A09 | 同工作区读写竞争 | 排他规则生效，跨会话也不能绕过；不同工作区可在总容量内执行 |
| A10 | 全局停止 | 不再开始新 Worker 或新的协调模型请求；运行中工具按规则取消，残留/未知结果如实报告 |
| A11 | 单 Worker 停止 | 仅该 Worker 收到局部取消；兄弟任务继续；旧尝试未结束前不启动替代写入 |
| A12 | Worker 运行中修订任务 | 旧队列失效，旧 Worker 后续副作用被拒绝；迟到报告只归档，不变成当前验收证据 |
| A13 | 多个问题、定向回复 | 回答只解除对应问题，修订影响按既有规则传播；不把回复错发给另一个 Worker |
| A14 | 连续进展和重复结束事件 | 进展不触发模型轮询；结果按键合并；事件队列和请求上下文有界 |
| A15 | 没有新事件 | 在可控时钟推进下模型请求数不增加；定时器只处理已获准的超时/重试 |
| A16 | 主 Agent 多次被唤醒 | 始终只有一个决策轮次；总请求与费用不重置；实际请求记录含本轮事件摘要 |
| A17 | Worker 达到上限 | 中文原因清楚、任务摘要简短、错误不重复；已有成果可见且交付给父协调者 |
| A18 | Worker ready，但验收失败 | 父任务保持未完成；最终 UI 不显示全部完成 |
| A19 | interactive 模式重启或打开历史 | 仅恢复视图，无模型调用和新副作用；显式继续后核对旧执行，再处理可执行队列；service 模式另测 B08/B09 |
| A20 | 两进程争用同一会话 | 第二执行所有者被拒绝或只读；旧代次提交不能覆盖新运行 |
| A21 | 慢/断开的 UI 订阅者 | Worker 和持久提交不被拖住；恢复快照后状态一致，控制端关闭遵守已声明策略 |
| A22 | 分支、fork、clone | 历史可读；没有克隆活跃执行权、重复自动唤醒或跨分支污染 |
| A23 | Plan、只读和审批拒绝 | Orchestrator、Worker 与调度器均无法绕过执行边界；拒绝后无未授权副作用 |
| A24 | 真实进程与后台工作停止 | 等待/清理的是实际线程和子进程，不只是取消 async 包装；不能提前释放工具注册表 |
| A25 | 旧会话和旧客户端 | 不丢历史、不误报完成；未协商异步能力时行为明确且兼容 |

时序测试使用 barrier、事件和可控时钟，避免依赖“睡眠若干毫秒大概已完成”。模型替身用于验证宿主机制；真实模型的拆分质量和及时收尾效果另做有预算、获授权的评测，不用离线 smoke 成绩代替。

## 16. 测试与最终交付

先运行新增调度模块和受影响模块测试，再扩大到共享执行入口的调用方。建议新增 `tests/orchestration/`，并至少覆盖现有的任务编排、会话恢复、执行恢复、执行边界、子 Agent、服务端和终端测试。

仓库根目录：

```powershell
python -m ruff check pepsicode tests
python -m ruff format --check pepsicode tests
python -m pytest -q
python scripts/check_project.py
python -m pepsicode.evals --mode smoke
```

桌面端 `pepsicode-client/`：

```powershell
npm test
npm run typecheck
npm run build
```

QQ 通道 `integrations/qq/`：

```powershell
npm test
npm run test:e2e
```

- [ ] 交付实现代码、状态/协议/存储迁移说明、上述场景的测试对应关系和实际测试结果。
- [ ] 交付两段演示：Worker 未结束时主 Agent 处理新事件；中断恢复时无自动副作用重放。
- [ ] 同时记录 Windows 与 POSIX 的进程退出、路径、锁及恢复验证情况；未验证的平台明确标注。
- [ ] 对测试失败标明新增回归、已证实的既有问题或环境限制，保留复现方法；不得删用例或降低断言。
- [ ] 说明真实模型评测是否执行、使用预算及观察到的限制；未执行不得写“真实模型效果已验证”。
- [ ] 所有核心验收通过后才将本清单对应项标为完成；界面有侧栏、能同时启动两个 Worker 或最终答案正确，都不足以单独验收异步编排。
- [ ] 完整目标必须同时通过 M2–M4 的第 21 节验收；测试报告分别标注宿主版本、平台、运行模式、隔离等级和授权策略版本。

## 17. 交付记录模板

| 批次/要求 | 实现位置 | 验证命令或演示步骤 | 实际结果 | 未验证项/剩余问题 |
| --- | --- | --- | --- | --- |
| 第一批 | 待填写 | 待填写 | 待验证 | 待填写 |
| 第二批 | 待填写 | 待填写 | 待验证 | 待填写 |
| 第三批 | 待填写 | 待填写 | 待验证 | 待填写 |
| 第四批 | 待填写 | 待填写 | 待验证 | 待填写 |
| 第五批 | 待填写 | 待填写 | 待验证 | 待填写 |
| 第六批 / M2 | 见「2026-11-14 M2 实施记录（第六批，补记）」及第 18 节叙述 | `python -m pytest -q tests/core/test_m2_environment.py tests/core/test_m2_process_channel.py --basetemp=<独立临时目录>`；全量 `python -m pytest -q --basetemp=<独立临时目录>` | M2 专项 15 项通过（真实 Git 仓库与真实子进程）；全量 1,450 通过、5 跳过、2 既有失败；ruff/格式/项目检查/smoke、桌面 83 项与 typecheck、QQ 单测与 e2e 2 项通过 | hardened 隔离、CPU/内存/网络等资源硬限制、端口自动分配未实现；POSIX 未实测；pytest 默认临时根 `pytest-of-Administrator` 有 WinError 5 环境问题，复现需独立 `--basetemp` |
| 第七批 / M3 | 见「2026-11-13 M3 实施记录」「2026-11-14 M3 补充记录：TUI attach 入口」 | `python -m pytest -q tests/server tests/service tests/core/test_orchestration_store.py`；`npm --prefix integrations/qq run test:e2e`；服务生命周期演示 | 定向 103 通过 1 跳过；QQ 单测 28、e2e 2 通过；全量 1,422 通过、2 既有失败；TUI attach 控制台 11 项通过（含真实回环 B08） | 真实 GitHub 凭据、真实 IM 无人看护、远程部署、POSIX 服务管理、全屏 TUI 经服务驱动轮次未验证 |
| 第八批 / M4 | 见「2026-11-14 M4 实施记录」 | `python -m pytest -q tests/core/test_m4_delivery.py tests/core/test_m4_external_delivery.py`；临时仓库 + 本地 bare 远端推送幂等演示；`python -m pepsicode.evals --mode real --track preset --case cross_file --case resume --case steering` | M4 专项 16 项通过；M2/编排/验收/恢复回归 79 项通过；真实模型小样本评测 1/3 通过（21 次请求、27.8K/13.4K tokens），失败类型：连接停滞、false completion | 真实 GitHub/CI、POSIX 交付链路、M4 交付链路的真实模型评测未验证 |

发生范围变化时同步修改能力边界、默认配置、验收矩阵和用户文档，不用仅修改某条提示词来宣称新能力已经交付。

## 18. M2：独立工作区、进程与安全隔离

[准备入口](../../pepsicode/orchestration/environment.py)按 WorkItem 持久化提交、分支、checkout 和准备阶段。[回归测试](../../tests/core/test_m2_environment.py)使用临时 Git 仓库，验证两个 worktree 可独立修改同名文件并分别启动检查进程，主目录的未提交改动保留。回归也覆盖分支冲突、checkout 归属变化、未知结果停止重放和拒绝 hardened。Windows 编排与 worktree 回归共 69 项通过，最终归属检查后的 M2 专项 4 项通过。

[受管进程通道](../../pepsicode/orchestration/process_channel.py)通过执行层启动受信任的子进程，校验 IPC 归属和顺序，限制帧大小和消息数，按尝试记录 PID、退出码和收尾。[脚本子进程测试](../../tests/core/test_m2_process_channel.py)验证一个进程退出时兄弟进程仍能返回，两个进程的宿主调用共享预算。

[Worker 模型和工具代理](../../pepsicode/orchestration/process_worker.py)已接入宿主显式开启的预备环境执行模式。Worker 在独立子进程中推进轮次，模型请求和工具执行由宿主仲裁，命令使用每 WorkItem 的临时目录与白名单环境。交互端自动准备、依赖和端口分配、资源硬限制与 hardened 仍待实施。

Windows 最终 M2 专项 15 项通过。覆盖实际 Worker 的并行写入和命令检查、应用层越界拒绝、宿主模型请求记录、进程退出时停止待处理的宿主操作，以及崩溃后保留 checkout 和兄弟结果。相关编排、子 Agent、执行边界和恢复联合回归 102 项通过、1 项跳过。

### 18.1 隔离等级必须分别定义

| 层级 | 实际保证 | 不能据此声称 |
| --- | --- | --- |
| 上下文隔离 | 每个 Worker 有独立历史、工具结果与压缩状态；通过摘要/引用交换成果 | 对文件、系统进程或凭据的访问受到 OS 限制 |
| 工作区隔离 | 每个实施 WorkItem 有独立 worktree、分支、输出和依赖环境 | 同一用户启动的任意命令无法读取兄弟目录；Git 公共元数据完全隔离 |
| 进程隔离 | Worker 使用独立 PID、受管进程树、可单独停止，异常可被宿主观察 | 容器/内核安全沙箱、全宿主故障免疫、共享磁盘永不被耗尽 |
| hardened 安全隔离 | 经验证的 OS 后端限制文件、网络、进程和资源访问，超范围操作实际被拒绝 | 任意恶意代码绝对安全、所有外部服务可回滚 |

- [ ] UI、运行记录和交付报告同时标明配置等级与实际生效等级，不能把 `cwd`、提示词中的“不得越界”或路径包装器标作 hardened。
- [ ] 完整隔离目标至少在一个声明支持的平台上通过 hardened 验收。其他平台没有对应后端时明确拒绝 hardened 运行，不能静默退化成直接启动命令。
- [ ] Windows Job Object/POSIX 进程组可用于资源管理与停止进程树；文件/网络访问限制要通过实际可用的隔离后端另外实现和验证。
- [ ] 指定服务账户、Worker 账户/容器身份、挂载范围和网络出口策略。若只实现进程/工作区隔离，交付必须停留在相应等级，不声称系统级安全隔离已完成。

### 18.2 每项实施工作拥有独立环境

- [x] 使用稳定 `work_id` 分配 worktree/分支；重试 Attempt 可以在核对后复用同一工作环境，两个有效 WorkItem 不能同时写同一分支或目录。见 [environment.py](../../pepsicode/orchestration/environment.py) 与 2026-11-14 M2 实施记录。
- [x] 保存仓库真实身份、基线 commit SHA、分支、worktree 路径、预期 HEAD、运行修订和准备状态。恢复时校验它们，不以“目录存在且含一个有效 HEAD”判定准备完成。见 [environment.py](../../pepsicode/orchestration/environment.py) manifest 与复验。
- [x] 记录 prepare_pending/preparing/prepared/prepare_failed 等环境状态和每个准备步骤的结果。准备失败不能被下次启动跳过，也不能删除未核对的部分产物后盲目重来。见 `test_failed_checkout_is_retained_and_not_replayed`。
- [x] 新分支使用项目约定的命名空间，创建前检查归属与碰撞。不得通过 `git worktree add -B` 或类似强制重置行为覆盖用户/其他任务的既有分支。见 `test_preparation_refuses_an_existing_branch`。
- [x] 主工作区可能有未提交修改。默认从明确的已提交 SHA 创建任务环境，不擅自 stash、提交、复制或回滚用户改动；任务需要这些改动时先确定可追溯的输入快照方式。见 `test_worktrees_have_distinct_checkouts_and_keep_dirty_main`。
- [ ] 每个工作环境有独立可写依赖目录、构建输出、临时目录、端口分配和测试数据。共享包缓存只采用已声明的只读/带锁方式，防止交叉安装、清理或写污染。
- [ ] 默认禁止把 `.venv`、`node_modules` 等可写目录通过 symlink/junction 指向主工作区；允许共享时必须证明符合所声明隔离等级。
- [ ] 默认不复制 `.env`、用户凭据或全部进程环境。通过最小化的配置/凭据通道供应当前任务所需能力，作用域和有效期可审计。
- [ ] 复用 `pepsicode/worktree/setup.py` 前必须改造其默认本地配置复制、Git hook 共享、`.worktreeinclude` 和依赖符号链接行为；既有交互便利功能不能直接成为无人看护 Worker 的默认准备策略。
- [ ] 依赖安装、Git hook、构建脚本和测试本身都是可执行代码，统一经过执行层、网络/路径策略和资源限额；不能因来自仓库就视为可信授权。
- [ ] worktree 共享的 `.git` 元数据由宿主受控 Git 入口管理并串行修改；hardened 环境不能把整个公共 Git 管理目录直接作为任意可写挂载。需要时使用独立 clone 或受限 Git 服务。

### 18.3 Worker 进程与公共服务

- [x] 每次有效 Attempt 有独立 Worker 进程身份和受管 IPC。Orchestrator 与调度服务不与 Worker 共用可被其任意修改的消息历史或运行内存。见 [process_worker.py](../../pepsicode/orchestration/process_worker.py) 与 2026-11-14 M2 实施记录。
- [x] 跨进程事件含运行、尝试、宿主代次与消息序号，IPC 进行身份校验和载荷限长；Worker 不能冒充用户审批、修改兄弟任务或操作宿主账本。见 [process_channel.py](../../pepsicode/orchestration/process_channel.py) 帧协议与 `test_invalid_child_frames_never_reach_host_operations`。
- [x] 预算与权限由唯一宿主执行服务仲裁。不能把 `RequestBudget` 序列化给多个进程后让各自独立扣账；模型请求需原子取得共享额度并按请求 ID 结算。见 `test_crashing_worker_does_not_stop_sibling_and_both_charge_host_budget` 与 `test_prepared_process_keeps_provider_request_and_accounting_in_host`。
- [x] 模型调用、工具执行、Git 和外部服务仍经过 `execution/` 对应入口。Worker 进程化不能绕开请求日志、审批、未知副作用或取消传播。见 `test_prepared_process_keeps_provider_request_and_accounting_in_host`（`model/prepared` 记录一致且 API key 不落 journal）。
- [ ] 设置并测试每 Worker 的 CPU/内存/进程数、运行时间、输出字节、磁盘使用和网络限额；无法强制的限额要标为仅监测，不得宣称已经硬隔离。
- [x] 单 Worker 异常退出时宿主捕获退出码，关闭其资源，保留工作区和结果，生成一次有归属的事件；兄弟任务保持运行。宿主崩溃、磁盘故障和系统 OOM 等共享故障另走整体恢复。见 `test_crashed_prepared_worker_leaves_sibling_and_checkout_available`。
- [x] “没有新进展”与“进程死亡”分别判断。模型长响应或测试运行不能仅因暂时没有文字输出就被当成死亡；超过已配置 deadline 后按取消与核对流程处理。见 [process_channel.py](../../pepsicode/orchestration/process_channel.py)（deadline 判定，无输出静默判死）与 `test_silent_worker_is_reaped_at_deadline`。
- [ ] hardened 测试必须覆盖绝对路径、`..`、symlink/junction、用户目录、兄弟工作区、凭据、未授权网络和子进程逃逸路径；不能只测试内置 read/write 工具的路径检查。
- [ ] Worker 与环境保留策略按任务结果明确配置；有未交付改动、未确认副作用或未核对归属时不自动清理。清理仍经过执行层并验证真实路径。

## 19. M3：常驻服务与低打扰的事件驱动协作

### 19.1 服务拥有任务，客户端负责交互

- [ ] 增加独立于 TUI/Electron 的服务进程，提供 start、stop、status/health、attach/detach 或等价入口。不同平台使用明确支持的服务管理方式，不把一个 daemon 线程称作系统服务。
- [ ] 服务持有队列、运行所有权、预算和 Worker 进程；客户端只经鉴权接口提交意图、查询状态、回答问题和执行控制。
- [ ] service 模式下关闭客户端、浏览器或 SSH 连接不取消已授权任务。重新连接从持久快照和事件游标恢复，不能丢失离线期间的问题与成果。
- [ ] UI 清楚显示运行所在主机。关闭用户笔记本后继续工作需要执行服务位于另一台仍在线的主机；服务也在该笔记本上时，关机后只能等重启恢复。
- [ ] 显式停止服务要持久化人工停止标记，阻止外部守护程序擅自继续已停任务；停止任务、停止服务、断开客户端是三种不同动作。
- [ ] 服务单实例与运行代次防止重复启动；服务健康分项显示存储、队列、模型、Worker、连接器和通知故障，不能只因端口可访问就报告健康。
- [ ] 进程异常重启先检查执行日志、存活资源、租约、预算和授权策略。只有可证明仍获授权且可安全继续的工作才能恢复；未知副作用进入核对。
- [ ] 不新增无鉴权的公网控制接口。远程部署、服务账户、访问控制和凭据轮换有明确部署说明与测试；本地绑定和受控隧道也需校验访问身份。

### 19.2 低打扰基于具体预授权

减少打扰的机制是“已授权范围内自动执行 + 宿主边界强制”，不能用全局跳过审批实现。

- [ ] 无人看护策略持久记录用户、仓库、目标任务、允许目录/工具/命令类别、网络目标、费用/次数/时间上限、允许的外部动作、有效期、版本和撤销状态。
- [ ] 本地修改、安装依赖、运行测试、创建分支、推送指定远端/分支、创建或更新 PR 等分别描述授权范围；推送和外部发消息仍必须有针对动作的明确授权并通过执行层策略判定。
- [ ] Orchestrator 与 Worker 不能自行扩大授权。策略撤销、到期、权限收紧在下一副作用入口生效，排队动作重新校验；已有副作用不可宣称已回滚。
- [ ] 策略内的正常步骤无需逐步 y/n；缺少授权时进入待处理状态并合并通知，不伪装成普通模型错误反复尝试。
- [ ] 除交付和越权外，需求歧义、凭据失效、预算耗尽、无法核对的副作用和多次无进展也可请求人类处理。不能承诺人类只在最终验收出现一次。
- [ ] 授权内普通失败可以走有限修复和重试；不得自动追加预算、延长有效期、删除失败测试或放宽验收标准。
- [ ] 设置离线通知发件箱和重连补取，按任务/问题/交付版本去重，避免每个工具事件都发 IM。普通进展在面板可查，关键问题和结果通过已授权通道交付。
- [ ] 外部通知或 PR 请求发送成功与否未知时保留 unknown/待核对状态，不盲目重发制造重复消息或重复 PR。

### 19.3 GitHub、CI 和 IM 事件

- [ ] 首批支持 GitHub 请求/审查及 CI 状态事件，复用已有 QQ 通道作为控制与通知入口；飞书等新增平台采用同一连接器协议，单独明确实现及授权范围。
- [ ] 事件入口校验来源身份、允许的仓库/用户、消息 ID 和事件类型；附件、代码、评论和 CI 日志都是任务数据，不能直接变成新增授权。
- [ ] 使用持久游标/外部事件 ID、接收去重、重连补偿和退避。宿主可以按连接器能力轮询外部 API，不能让 Orchestrator 用模型循环轮询。
- [ ] CI 结果绑定仓库、PR/分支、head SHA、检查名称和运行 ID。旧提交的成功不能覆盖新提交失败；重复和乱序通知不能触发重复修复。
- [ ] 审查意见与原任务范围关联。属于同一目标的修复在原工作项上产生新修订；新增需求交回 Orchestrator 重新判断，不任意扩大 Worker 的任务。
- [ ] 首版不因看到任意 Issue 就主动开发。新工作必须来自允许用户的明确请求或已获授权的具体规则，且限制同时接收的任务数量。
- [ ] 服务工作不依赖 IM 一直连接。通知失败不能丢失结果；缺少必须的人类回答时只暂停依赖它的工作，不让无关任务无故停摆。

## 20. M4：面向软件工程的交付闭环

### 20.1 可复现的实施与修复

- [ ] 每项实施工作形成环境清单：base SHA、锁文件摘要、安装/测试命令、运行时版本、必要环境变量名、网络策略与测试数据来源；不记录凭据值。
- [ ] 工作流覆盖环境准备、实现、项目检查、任务验收、有限修复和交付。每步保存实际输出引用、退出状态、耗时和对应代码版本。
- [ ] 区分代码失败、环境/依赖失败、模型/传输失败、权限阻塞、预算限制与未知副作用，按原因选择下一步，不把所有失败都交给同一个无界重试循环。
- [ ] 代码或测试失败时，Worker 可在授权范围内读取日志、定位并修改，再执行针对性检查；无进展判据参考代码/条件变化和失败指纹，单纯改日志不能重置次数。
- [ ] 不能通过删除失败测试、跳过有问题模块、放宽类型/断言、静默更换验收命令或修改预期来制造成功；确需改变验收须按父任务修订和授权规则处理。
- [ ] 长任务压缩保留任务约束、当前基线、已执行动作、证据引用、失败原因和未完成事项。不同 Worker 不广播完整历史，只交付有界摘要和按需可读的结果引用。
- [ ] 测量每种实际工作流的模型请求、输入/输出 token、压缩次数、峰值上下文、CPU/内存/磁盘和完成耗时。优化效果以可复现实测报告为准，不宣称所有场景固定省多少 token。

### 20.2 集成、冲突与精确版本验收

- [ ] 每个 Worker 交付自己的 commit/diff、base SHA、head SHA、变更说明、测试证据和已知问题；不得直接写入其他 Worker 的工作区或用户主分支。
- [ ] 汇总多个成果时，由受管集成任务在独立集成工作区按明确顺序处理；Git 元数据变更和集成串行执行，保留各来源结果。
- [ ] 无冲突合并/cherry-pick 也需运行集成后的验收。各 Worker 独立测试通过，不代表组合后的系统通过。
- [ ] 有冲突时创建范围明确的解决任务或请求人类判断；禁止为了消除冲突自动丢弃一方改动、强制推送或 reset 用户分支。
- [ ] 验收绑定准确的候选 head SHA、任务修订、测试/验收定义和工作区摘要。基线、需求或候选代码发生变化后重验受影响证据。
- [ ] 对交付物进行检查可以用宿主验证或独立复核 Worker；最终状态由证据计算，不能由提交代码的 Worker 一句话批准自己完成。

### 20.3 PR、终审与失败处理

- [ ] 默认先交付本地候选分支/diff及验证结果。有明确推送/建 PR 授权时，才通过执行层向指定仓库和目标分支交付，记录外部动作幂等身份与 PR 地址。
- [ ] PR 中描述需求、改动、实际验证、未完成项和限制；推送新版本后重新关联 CI 和审查，不继承旧 head 的验收或审批。
- [ ] 默认不自动合并主分支、发布版本或部署生产。若将来启用，必须提供针对目标动作的授权与宿主检查，绑定准确 head、审查/CI 条件和策略版本；提示词约定不能单独作为门禁。
- [ ] 用户目标若是“提交 PR”，创建并验证对应 PR 可以满足其交付条件；若目标包含“合并/上线”，PR 建立本身不能标记整个任务完成。
- [ ] 回滚按副作用分类：可丢弃的临时环境、可恢复的代码分支和已发生的外部服务动作分别处理。停止 Worker、关闭任务或删除 worktree 都不等于外部动作已撤销。
- [ ] 保留失败环境、未提交代码和完整证据引用供核对；清理任务不能删除唯一成果。自动清理需限定归属、保留期和安全条件。
- [ ] 最终交付把成果、证据、待人工动作和保留资源合并为一份精简通知，正常过程可查询而不反复打扰。

## 21. M2–M4 的补充验收场景

这些场景与 A01–A25 一同构成完整目标的验收矩阵。

| 编号 | 场景 | 通过标准 |
| --- | --- | --- |
| B01 | 两个 Worker 并行改同名文件 | 各自 worktree/分支得到各自改动；用户主工作区和兄弟目录不被污染 |
| B02 | 依赖安装、端口和输出冲突 | 可写环境独立，缓存使用符合策略，端口有归属；互不覆盖或误清理 |
| B03 | Worker 原生进程崩溃 | 宿主及兄弟任务继续；该执行有退出事件、日志和保留成果，外部未知动作不被重放 |
| B04 | 越权文件/进程/网络访问 | hardened 模式实际拒绝绝对路径、symlink/junction、兄弟目录、用户凭据和未授权网络访问；不只测试包装器 |
| B05 | 所选平台缺少沙箱后端 | hardened 启动明确失败；没有静默退化或仍显示“已安全隔离” |
| B06 | 资源耗尽与共享预算竞争 | 单 Worker 命中可强制的资源限额时被收敛；跨进程请求不超共享预算，未支持的限制如实标识 |
| B07 | 准备中断与分支碰撞 | 部分准备不会被当成成功；已有分支、用户未提交修改及其他 worktree 不被重置 |
| B08 | 关闭所有客户端 | service 模式已授权 Worker 继续；interactive 模式按原策略停止；重连看到准确成果与问题 |
| B09 | 常驻服务崩溃并重启 | 按有效授权恢复安全任务；预算不清零；未知副作用等待核对；旧进程代次不可继续提交 |
| B10 | 人工停止/撤销授权 | 守护管理器不重启已明确停止的任务；撤销后不开始新副作用，迟到事件不恢复授权 |
| B11 | 宿主关机与远程主机运行 | 本机停机不会虚报仍运行；远程宿主在线时客户端关机不影响它，状态显示真实执行位置 |
| B12 | 重复、乱序 GitHub/CI 事件 | 同一事实不重复派工；旧 SHA 成功不能通过新 SHA 验收；陌生来源不获得任务权限 |
| B13 | 无人看护期间测试失败 | 在已授权范围内有限修复并重测；超预算或无进展后只发合并后的明确处理通知 |
| B14 | 合法任务中出现新权限需求 | 原策略内继续，无关任务不受影响；越界步骤等待明确处理，不暗中切到全权限 |
| B15 | 两个独立成果集成 | 独立测试与集成测试均有版本证据；出现冲突时保留双方改动，不能强制丢弃 |
| B16 | 推送/建 PR 响应丢失 | 按仓库/分支/head及外部记录核对；不盲目重发或产生重复 PR |
| B17 | 基线或候选提交发生变化 | 旧测试/审查不继承为当前通过；重新验收实际交付的 head |
| B18 | 长任务压缩与恢复 | 约束、来源、已执行动作和证据仍可追溯；不把各 Worker 全部历史广播给全部 Agent |
| B19 | 通知断线与事件风暴 | 过程更新留在面板；关键问题/交付可靠补取且去重；没有逐工具 IM 轰炸或无界模型唤醒 |
| B20 | 交付后的资源保留与清理 | 成果和证据仍可访问；清理只处理已确认归属资源，不删用户数据或唯一未提交修改 |

验收报告必须区分“机制通过脚本测试”“界面/服务实际运行验证”和“真实模型软件工程任务评测”。通过前两者不自动证明已具备生产环境可靠性，也不能替代对真实任务成功率、失败恢复和隔离边界的测量。
