# 真实编码任务评测

评测入口是 `python -m pepsicode.evals`。它在新建的小型 Python 项目上运行真实的模型适配器、Agent Loop 和自动验证修复，再由模型工作目录之外的检查程序验收。它衡量任务行为，区别于 `benchmarks/` 中的渲染、token 估算等性能基准。

## 快速运行

先运行无网络、无凭据的冒烟检查：

```powershell
python -m pepsicode.evals --list
python -m pepsicode.evals --mode smoke
```

`smoke` 使用专门的脚本代理完成一道加法题，只验证工具、进程、公开测试、独立验收和报告链路。报告的 `success_rate`、token 和费用为 `null`，不代表真实模型效果。

真实模型评测读取现有 Pepsicode 配置及环境变量；需要有效的模型 ID 与 API 凭据。它会发起收费的模型请求。默认每个任务运行一次，可显式增加重复次数：

```powershell
python -m pepsicode.evals --mode real --case pagination --runs 3
python -m pepsicode.evals --mode real --runs 3 --timeout 180 --max-requests 50 --budget 1
```

`--case` 可重复指定；不指定则运行当前全部 12 个场景。`--output` 可指定一个尚不存在的结果目录，已有目录不会被覆盖。默认输出到 `.evals/<UTC时间戳>/`，已加入 `.gitignore`。

验收有两条轨道：默认 `--track preset` 使用预置公开检查或声明的产物条件；`--track model` 不注入这些任务验收条件，由模型自行定义。项目回归检查仍独立保留。两条轨道使用同一个工作目录外检查器，必须分开报告，不能将预置条件轨道的结果称为模型自主验收能力。

```powershell
python -m pepsicode.evals --mode real --track preset --runs 3 --output .evals/preset
python -m pepsicode.evals --mode real --track model --runs 3 --output .evals/model
```

来源也单独标记：`evaluation_kind` 为 `offline_smoke`、`scripted_service` 或 `provider`。模拟 HTTP 服务集成应传 `--scripted-service`，或由评测调用方设置 `runtime._evaluationSource="scripted_service"`。脚本服务的行为通过率可记录为 `behavior_success_rate`，但 `success_rate`（真实提供者模型效果）保持 `null`；测试不能因为走了真实适配器就变成真实模型评测。

来源由调用方如实声明；框架无法仅凭 HTTP 响应证明远端确实使用模型生成。对外比较时仍须复核服务配置和原始轨迹。

## 首批任务

| Case | 工作内容 | 独立验收重点 |
| --- | --- | --- |
| `pagination` | 修复分页边界 bug | 不同页长和集合大小、越界、非法参数、输入不变、接口不变 |
| `cross_file` | 新增订单取消功能并更新报表 | 跨文件行为一致、重复取消、禁止取消已付款订单、收入和数量计算 |
| `compaction` | 在两次强制语义压缩后实现金额计算 | 早期消息尾部的 Decimal、舍入、校验和文件保护要求仍生效 |
| `resume` | 一个进程保存需求和工作状态，另一个进程重新打开并完成任务 | 使用生产上下文持久化接口恢复需求，继续实现并验收 |
| `pagination_zh` | 自由中文行动请求 | 不能被当作普通问答结束 |
| `mixed` | 先解释再修改 | 解释后仍须完成修改部分 |
| `document` | 创建 CSV 使用文档 | 文档内容和产物存在，不强迫执行代码测试 |
| `analysis` | 只解释分页公式 | 保留文件、不强迫写代码或执行项目程序，能正常回答 |
| `no_tests` | 修复没有现有测试的项目 | 针对行为的单独检查，不用“零测试”代替验证 |
| `interrupted_resume` | checkpoint 落盘后进程以 `os._exit` 骤停，再启动新进程 | 不依赖清理函数恢复；仍是明确 checkpoint 位置的故障注入 |
| `multiple_unknown` | 两个已置入未知状态的调用 | 先观察真实收据文件，逐项核对；未引用调用和过期观察不能被误清空 |
| `steering` | 首次写入后从持久队列交付新目标 | 从返回 42 改为返回 43，旧条件须随真实输入修订，最终行为和交付记录由外部检查 |

压缩和恢复任务使用预设的调查历史；恢复任务是可控的 checkpoint 重新打开测试，不模拟进程在任意外部副作用中崩溃。首批任务是小型固定样本，不等同于大型开源仓库基准。一次运行不能证明稳定性，应按模型、思考档位和相同样本重复比较。

每次运行先检查原始项目确实不能通过独立验收，再执行代理。公开测试及 `currency.json` 等指定文件进行字节级保护检查。任务成功以工作目录外的行为检查、保护文件和场景要求为依据，不能用内部 `complete` 自我评分；预算/请求上限、等待用户、步数耗尽和取消另按执行限制记录失败。内部未完成但外部通过的结果另列，便于发现不必要阻塞。提前退出检查进程但没有完成验收标记，也不会被计为通过。

## 报告与统计

每个输出目录包含：

- `protocol.json`：完整计划样本、样本摘要、评测代码摘要、版本、模型、思考档位、运行限制和单价。
- `observations.jsonl`：逐次结果与失败原因。
- `report.json`：总体、按 case 汇总和逐次结果。
- `report.md`：简要报告。
- `<case>-<编号>/worker.json`：对话、工具活动、验证报告、压缩边界和每次模型请求的用量。摘要请求也在统计内。
- 各次运行的 `workspace/`、隔离的 `home/` 和独立检查脚本，便于复查。

正常任务失败、超时、步数或请求预算耗尽计为失败。缺失 worker 结果、配置或基础设施错误记为未评分；样本不完整时不发布总体成功率。缺失用量不能当成零费用。原 `human_interventions` 改名为 `clarification_waits`，不能用澄清次数代替所有人工负担。

报告分别提供审批请求数、后端决策数、后端等待时间、澄清等待、成本、耗时和请求次数。`false_completion_rate` 的分母是已评分且内部宣称完成的次数，分子是其中外部失败的次数；`task_failure_rate` 使用所有已评分任务作分母。问答阻塞率单列纯分析样本，恢复率以恢复场景或实际发生有界重建的已评分任务为分母。分母为空时为 `null`，基础设施错误的数量和原因单列。

评测使用自动审批：仅允许已声明的精确检查命令；未知操作样本还允许具有正确收据观察的逐项核对。其余请求拒绝。因此审批等待是模拟后端耗时，不是真人等待，也不能据此宣称生产环境人工负担低。独立检查失败但内部完成的轨迹明确计为误报完成。

对照工具不调用模型：

```powershell
python -m pepsicode.evals --compare .evals/before/report.json .evals/after/report.json --output .evals/comparison.json
```

它要求任务摘要、模型/档位、价格、限制、来源和验收轨道一致；不完整样本拒绝生成配对估计。输出逐样本结果及探索性的配对 bootstrap 区间；小样本、任务相关性和授权策略仍须人工复核。旧版只比较双方支持的共同样本，缺少新 API 时须在评测侧适配，不能把新 Harness 补给旧版后声称是公平基线。当前保留了改造前离线复现与前后 smoke 原始报告，尚未执行付费真实模型配对实验，真实成功率收益尚未证实。

## 费用和运行限制

`--timeout` 是每次运行的代理阶段墙钟期限，含恢复准备及摘要调用；前置和最终独立验收另各有 30 秒上限。`--max-steps` 是每轮工具步数上限，自动验证修复最多三轮；`--max-requests` 是所有普通请求和摘要请求的合计上限。HTTP 瞬时失败最多重试一次，不计作新的逻辑请求。

`--budget` 是每次运行的美元**估算**上限，沿用 `CostTracker` 的近似价格表。开始下一次请求前检查，因此最后一次请求可能超出上限；用量缺失时仍由请求次数及超时限制执行。对于价格表之外的模型，默认价格只用于预算保护，报告费用保持 `null`。

可通过 `--pricing prices.json` 明确提供模型价格。文件按请求中的模型 ID 配置，每种价格单位为美元/百万 token，必须同时填写四项；摘要 fallback 模型使用它自己的条目。下面仅演示格式，数值需要替换成实际适用单价：

```json
{
  "your-model-id": {
    "input": 1.0,
    "output": 2.0,
    "cache_read": 0.1,
    "cache_write": 1.0
  }
}
```

```powershell
python -m pepsicode.evals --mode real --pricing prices.json --runs 3
```

## 运行边界

每次重复使用新的进程、工作目录和用户目录，不加载个人 Skills、Hooks、MCP 或持久记忆。模型凭据经 stdin 传递，不写入配置或协议报告，也不传入测试进程的环境变量。固定暴露文件读写、搜索、任务清单、澄清和公开测试工具；没有通用 shell、网络工具或子代理。因此结果代表这套固定工具配置，不代表用户的所有扩展组合。

超时会结束所属进程树。临时项目隔离和工具审批是应用层边界，不是操作系统安全沙箱；生成的 Python 与测试代码依然以当前用户身份执行。这里的独立验收用于避免模型自我评分，不声称能抵御主动攻击评测器的代码。对话和代码会保留在结果目录中。

## 开发验证

```powershell
python -m pytest -q tests/test_context_reliability.py tests/test_task_evals.py
python -m pepsicode.evals --mode smoke
```

集成测试使用带虚构凭据的本机模拟 Provider，验证完整请求链路及上述场景，不调用付费模型。CI 只运行离线测试与 smoke，不自动启动真实模型评测。
