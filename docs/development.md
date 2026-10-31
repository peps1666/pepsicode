# 设计与开发

> 设计取舍、测试、构建与真实任务评测。

[← 返回 README](../README.md)

## 设计取舍

跨项目的 Agent 工作规范由 `pepsicode/prompts/AGENTS.md` 随应用打包加载；仓库根目录的 `AGENTS.md` 仅约束在 PepsiCode 仓库中开发时的工作，不会作为其他项目的通用目录说明。

几条贯穿全项目的取舍，读代码或写扩展时可以拿它当判断依据：

- **确定性优先。** 关键的前置 Hook 同步执行，能在权限检查前确定性阻断；只有非前置的后台动作交给受管线程池，并在进程退出时收敛。
- **上下文是稀缺资源。** 工具 schema 按需加载，大输出转 artifact，只把有界预览交给模型；压缩不删除已归档的原始消息。
- **权限不能外借。** Hook 配置不能声明免审批，`deny` 在完全访问下依然生效，Plan 模式无条件禁用命令 Hook。
- **失败要可见。** 无测试、被拒绝、未执行的检查都不会被算作通过；验证无进展、压缩无进展都会显式停止而不是静默重试。
- **没有魔法数字。** 模型、思考档位、缓存能力和上下文容量都以接口或显式配置为准，不按名称前缀猜测。

## 开发与验证

```bash
python -m ruff check pepsicode tests
python -m pytest -q
```

行为评测与离线冒烟：

```bash
python -m pepsicode.evals --list
python -m pepsicode.evals --mode smoke
# 使用已配置模型执行真实任务（会产生模型调用费用）
python -m pepsicode.evals --mode real --runs 3
```

首批覆盖分页 bug、跨文件功能、两次压缩后继续工作和跨进程恢复，独立验收并输出成功率、耗时、token、费用估算与失败原因。Smoke 使用脚本代理，不计为模型效果。配置、统计边界和报告格式见[真实任务评测](task-evaluations.md)。

用户任务另有独立的状态、修订号与验收证据：实现类任务需要具体验收条件，运行中的需求变更会使旧证据失效；输入队列、Todo 和澄清问题随会话保存。处理流程及限制见[任务生命周期与验收](task-orchestration.md)。

后续修改按[Harness 修改清单](harness-improvement-plan.md)持续跟踪：完成项以 `[x]` 标记，记录实现和回归证据；待真实评测及平台验证的项目保持未完成。

Hooks v2 专项测试：

```bash
python -m pytest -q tests/test_hooks_v2.py
```

CI 在 Ubuntu 与 Windows 上跑 Python 3.11 / 3.12 矩阵，包含 ruff 检查、格式检查与全量测试。贡献说明见 [docs/CONTRIBUTING.md](CONTRIBUTING.md)。

主要目录：

```text
pepsicode/
  main.py                CLI 入口
  config.py              配置加载
  tooling.py             工具协议与注册表
  permissions.py         权限与 Plan 模式边界
  core/                  代理引擎：agent_loop（主循环）、session、state、任务编排
  llm/                   模型层：anthropic_adapter、mock_model、重试与成本统计
  context/               上下文层：压缩恢复、记忆系统、系统提示词、skills
  intelligence/          语言服务接入与代码智能
  cli/                   命令层：斜杠命令、管理 CLI、安装器
  server/                WebSocket 服务端（python -m pepsicode.server）与协议定义
  hooks/                 Hooks v2 模型、加载、信任、引擎与运行时接线
  tools/                 内置工具
  agents/                子代理定义与追踪
  approval/              审批后端
  sandbox/               命令沙箱分类
  worktree/              Git worktree 会话管理
  tui/                   终端界面（app.py 为全屏 TUI 主程序）
pepsicode-client/        Electron 桌面客户端
  electron/              主进程（main.ts）与预加载（preload.ts）
  src/                   React 前端源码
    conversation/        聊天界面组件
    sidebar/             会话列表与项目选择
    stores/              Zustand 状态管理（session / connection）
    theme/               白色主题设计令牌
docs/                    agent-capabilities.md 及审计报告
tests/
```
