# 项目问题首批修复验证

日期：2026-10-27。基线：`af533e915c3d7baafcaf09c45f30030d2d30f871`；验证对象为提交前的 2.0.7 开发工作区。修复与本记录一同提交；验证时完整生产源码摘要及 dirty 身份见 [smoke 协议](smoke/protocol.json)，不改写为提交后实验。问题状态见 [项目问题文档](../../../docs/project-issues.md)。

## 回归与范围

| 检查 | 结果 | 原始记录 |
| --- | --- | --- |
| 改造前独立探针 | 6 failed | [基线输出](regression-before.txt) |
| 相同 6 个探针修复后 | 6 passed | [修复后输出](regression-after.txt) |
| 完整 Python 回归 | 1057 passed / 4 skipped | [全量输出](python-full.txt) |
| 构建身份、P1 与编排专项 | 51 passed（早于最后的工具影响投影小修，最终全量覆盖该小修） | [专项输出](python-focused.txt) |
| 客户端完整测试 | 75 passed | [客户端输出](client-tests.txt) |
| TypeScript / Ruff / 候选版本与入口链接 | 本地通过 | `npm run typecheck`、`ruff check/format --check`、`python scripts/check_project.py` |
| 离线 smoke | 1 passed；0 次模型请求；模型成功率为 null | [报告](smoke/report.json) |
| 源码 wheel 离线构建与目标目录安装 | 成功，安装包版本为 2.0.7 | [安装输出](source-install.txt) |

原始缺陷包括 build/dist/release 的绿色证据被后续命令破坏、迁移删除源文件被误拦，以及真实指令放宽/收紧没有同步分类。回归还覆盖同时间戳覆盖、新增删除、缓存写入、显式目录、角色纠正和旧记录、基线保护、重复/撤回输入、保存失败回滚、只读与 Plan 不扩大权限。全量中的 4 个跳过项没有计为通过，两个历史警告来自中文输入测试返回非 None。

测试使用隔离 USERPROFILE/TEMP 与本机脚本 HTTP 服务，无付费模型请求。源码安装使用 `pip install --no-deps --no-build-isolation --no-index --target`，复用现有环境中的依赖，不是纯净机器依赖安装验证。从目标安装目录运行 `python -m pepsicode --version` 得到 `2.0.7 (development, unknown)`，不会借用安装目录上层的项目提交。无服务商配置时诊断如实报告缺模型；离线 smoke 不依赖真实配置。

## 快照计量

可重复脚本：

```powershell
python scripts/benchmark_workspace_state.py --baseline af533e9 --samples 3 --output .eval-work/new-snapshot-profile.json
```

输出文件必须不存在。脚本读取基线源码，在相同当前输入上比较旧/新扫描器，不执行模型或用户命令。源码包是真实项目子目录；仓库根有超过 512 MiB 的历史成品，按上限拒绝扫描，不重新排除 build/dist/release 来通过。

- [首次测量](snapshot-profile.json)：1 万文件中位数 11156 → 7922 ms；源码包 168 个文件、1547376 字节，156 → 109 ms。单任务探针 5 次扫描 546 ms，1 次模拟 task 授权。
- [带版本身份的复测](snapshot-profile-delivery.json)：1 万文件中位数 10453 → 7703 ms；源码包 169 个文件、1548608 字节，141 → 94 ms。单任务探针 5 次扫描 578 ms，1 次模拟 task 授权。记录了测量时生产源码摘要；之后的工具影响意图小修不改变扫描算法，不能把该摘要当作最终候选摘要。

每个样本读取实际内容，未以 mtime、文件大小或不完整 watcher 作为唯一依据。scandir 类型信息和同一次 manifest 内重复路径可复用，不跨扫描信任旧内容。扫描指标的 `files` 包含目录条目；工作负载表单独统计文件数。固定顺序、热缓存、Windows 文件系统、计时精度和宿主负载影响数值；这些测量不证明真实模型吞吐或人类审批负担改善。

## 尚未验证或实施

常规客户端 CI 已配置，但本轮没有远端 CI 运行或分支保护变更。2.0.7 尚未固定到提交、构建或发布 ZIP；嵌入清单和 ZIP 身份对照仅有单元/接线验证。[候选发布清单](../../../docs/release-candidate.md) 保留待验收项。

付费真实模型配对实验、其余职责拆分、实际交互规模测量、原生受限后端、金额预算并发预留及受控 Git 观察继续按问题文档分批处理。工程回归与模拟服务结果不能替代这些结论。
