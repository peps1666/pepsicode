# AGENTS.md

PepsiCode 是本地编程代理：Python 内核供终端 TUI、Electron 桌面端和 QQ 通道共用。
修改前阅读 `docs/development.md`；涉及权限、执行或恢复时阅读 `docs/harness-boundaries.md`。

## 代码位置

- `pepsicode/core/`：Agent 循环、会话、任务编排与验证。
- `pepsicode/context/`：提示词、项目指令、上下文压缩、记忆与 Skills。
- `pepsicode/tools/`、`pepsicode/tooling.py`：工具实现与注册。
- `pepsicode/execution/`：文件、进程与外部服务执行入口。
- `pepsicode/llm/`：模型适配、请求、重试与用量。
- `pepsicode/server/`：WebSocket 服务与协议。
- `pepsicode/tui/`、`pepsicode-client/`：终端与桌面界面。
- `integrations/qq/`：QQ 私聊通道；`tests/`：Python 回归测试。

## 工程约定

- 共享执行规则放在内核；修改时检查各前端和通道的调用路径。
- 优先复用现有工具、Hook、Skills 和 MCP 接口；属于循环职责的行为才修改主循环。
- 内置工具的副作用必须经过 `execution/` 对应入口，不得绕过审批。
- 权限和 Plan 模式由执行层强制约束，不能仅依赖提示词。
- 父子任务共用预算、取消与截止时间；未知结果不得自动重放。
- 修改协议、配置或会话格式时，同步检查消费者与旧会话恢复。
- 模型能力使用接口或显式配置；工具输出和模型上下文必须有界。
- Python 支持 3.11+，格式遵循 `pyproject.toml`；路径、进程和编码兼顾 Windows 与 POSIX。
- 保留已有工作区改动，不夹带无关重构或依赖升级。
- 用户可见行为变化，同步更新对应文档。

## 验证

先运行受影响模块的测试；共享执行入口或跨模块改动扩大验证范围。
行为修复应补充能复现问题的回归测试。

仓库根目录：

```sh
python -m ruff check pepsicode tests
python -m ruff format --check pepsicode tests
python -m pytest -q
python scripts/check_project.py
```

Agent 工作流修改增加 `python -m pepsicode.evals --mode smoke`。
桌面端在 `pepsicode-client/` 执行 `npm test`、`npm run typecheck`；构建或资源变化增加 `npm run build`。
QQ 通道在 `integrations/qq/` 执行 `npm test`、`npm run test:e2e`。

只报告实际执行的检查。跳过、被拒绝或未发现测试不能算通过；
离线 smoke 不代表真实模型效果，真实模型评测按授权范围运行。

## 交付

简要说明改动、验证结果及未验证部分。
本文件只保留长期仓库规则，详细流程链接到对应文档。
