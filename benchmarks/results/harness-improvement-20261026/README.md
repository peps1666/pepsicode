# Harness 改造验证记录

日期：2026-10-26。改造前基线：`12fd294f7da6b891f0a9aef231db37ccc798e7b0`；改造后为本轮工作区，生产代码摘要见 `after/protocol.json`。实施状态见 [修改清单](../../../docs/harness-improvement-plan.md)。

本目录记录工程回归、离线 smoke 和本机微基准。没有调用付费真实模型，不能据此推断真实任务成功率、实际人工负担或模型吞吐量的提升。

## 正确性与集成验证

| 验证 | 结果 | 原始记录 |
| --- | --- | --- |
| 改造前 8 个最小复现 | 5 failed / 3 passed | [基线输出](../harness-baseline-12fd294.txt) |
| 改造后相同 8 个最小复现 | 8 passed | [回归输出](regression-after.txt) |
| 全量 Python 回归 | 1030 passed / 4 skipped | [完整输出](python-full.txt) |
| 随后评测来源标记修改的专项 | 40 passed | [专项输出](python-evaluation-final.txt) |
| 客户端测试 | 74 passed | [完整输出](client-tests.txt) |
| 静态检查 | Ruff check、format check、TypeScript typecheck 通过 | 本轮本地执行 |

Python 全量回归的两个已有警告来自中文输入测试返回非 None；4 个跳过项没有计为通过。HTTP 集成使用虚构凭据、本机脚本服务和隔离用户目录，覆盖 12 种任务场景；其中中断恢复在明确 checkpoint 位置骤停进程，中途改需求通过生产持久输入队列交付。它们验证接线与边界，不代表真实模型能力。

复现最小用例：

```powershell
python -m pytest tests/test_harness_improvements.py -q -k 'colloquial_actions or analysis_mentions or later_command or unrelated_readme'
python -m pytest tests/test_task_evals.py tests/test_harness_improvements.py -q
```

全量回归先于最后的评测来源标签修改；最后修改由上述 40 项专项覆盖。测试使用隔离的 USERPROFILE/TEMP，真实模型凭据从测试环境移除。

## 前后 smoke

`before/` 和 `after/` 保留原始协议与报告；[配对结果](improvement-paired-smoke.json) 由当前比较工具生成。共同样本、限制与模型设置相同，前后各 1 次均通过，模型请求数为 0。真实模型成功率及其差值保持 null。旧协议没有新字段，比较器按旧 smoke/preset 默认值兼容；旧版和新版摘要范围不同，不能直接比较摘要值。

```powershell
python -m pepsicode.evals --compare benchmarks/results/harness-improvement-20261026/before/report.json benchmarks/results/harness-improvement-20261026/after/report.json
```

## 本机微基准

- `budget-before.json`、`budget-after.json`：4 个线程，每个模拟请求等待 0.04 秒，重复 3 次。改造前当前实现与基线均串行，约 0.156 秒；无金额上限的锁优化后，当前中位数 0.031 秒、基线 0.172 秒。定时器粒度、调度与宿主环境影响绝对值，只证明该合成用例的并发锁行为。有金额上限仍串行，未实现费用预留。
- `snapshot-before-path-optimization.json`、`snapshot-benchmark.json`：读取内容并计算摘要，固定排除规则；优化了重复路径解析，未使用 mtime 缓存。工作区约 3.5 MB，中位数从 1031 ms 降为 360 ms；1 万个小文件共 2.88 MB，从 26750 ms 降为 10172 ms。两次工作区文件数为 388/389，适合量级参考；合成文件集相同。Windows 文件系统和执行环境开销明显，不能推广为其他机器的性能保证。

## 保留的未完成项

受控 Git 状态查询、真实模型配对实验、有金额上限的并发费用预留、原生受限执行后端及跨平台逃逸验证均未完成。应用层审批、内容快照和恢复记录不能替代操作系统隔离；当前文档及清单继续明确这些范围。
