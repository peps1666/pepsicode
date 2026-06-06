# pepsicode 快速上手指南

## 这是什么？

pepsicode 是一个**零依赖、纯 Python 标准库**的终端 AI 编程助手，对标 Claude Code。支持全屏 TUI 交互、30+ 内置工具、MCP 协议、Skills 系统、会话持久化等功能。

## 5 分钟速览

```bash
# 安装
python -m pepsicode.main --install

# 启动（mock 模式，无需 API key）
PEPSI_CODE_MODEL_MODE=mock python -m pepsicode.main

# 正常启动
python -m pepsicode.main
```

---

## 项目结构

```
pepsicode/
├── main.py              ← 入口：参数解析、组装组件、分派到 TTY 或管道模式
├── config.py            ← 加载 ~/.pepsi-code/settings.json + 环境变量
├── agent_loop.py        ← 核心循环：调模型 → 处理工具调用 → 重试/压缩
├── tooling.py           ← 工具抽象：ToolDefinition、ToolRegistry、ToolResult
├── types.py             ← 共享类型：ChatMessage、AgentStep、ToolCall
├── permissions.py       ← 权限系统：路径/命令/编辑审批
├── tty_app.py           ← 全屏 TUI 主控（2685行）
├── prompt.py            ← 系统提示词构建
├── context_manager.py   ← 上下文窗口追踪、token 估算、自动压缩
├── memory.py            ← 三级记忆：用户/项目/本地
├── session.py           ← 会话持久化：保存/恢复/索引
├── state.py             ← Zustand 风格 Store 状态管理
├── hooks.py             ← 生命周期事件系统
├── auto_mode.py         ← 自动模式：权限分级、注入检测
├── sub_agents.py        ← 子代理系统：explore/plan/general
├── skills.py            ← SKILL.md 技能发现和加载
├── mcp.py               ← MCP 协议客户端
├── anthropic_adapter.py ← Anthropic API 适配器
├── cost_tracker.py      ← 费用和 token 追踪
├── install.py           ← 交互式安装器
│
├── tools/               ← 内置工具（28个）
│   ├── read_file.py     ← 文件读取（带缓存）
│   ├── write_file.py    ← 文件写入
│   ├── edit_file.py     ← SEARCH/REPLACE 编辑
│   ├── multi_edit.py    ← 多文件批量编辑
│   ├── patch_file.py    ← 批量补丁
│   ├── modify_file.py   ← 全文替换
│   ├── list_files.py    ← 目录列表
│   ├── file_tree.py     ← 目录树
│   ├── grep_files.py    ← 内容搜索
│   ├── run_command.py   ← Shell 命令
│   ├── web_search.py    ← 网页搜索
│   ├── web_fetch.py     ← 网页抓取
│   ├── git.py           ← Git 操作
│   ├── task.py          ← 子代理委派
│   ├── ask_user.py      ← 用户选择器
│   ├── todo_write.py    ← 任务跟踪
│   └── ...
│
└── tui/                 ← 终端渲染层
    ├── chrome.py        ← 面板布局、边框、footer
    ├── input.py         ← 输入提示区
    ├── input_parser.py  ← 按键/鼠标解析
    ├── markdown.py      ← Markdown → ANSI
    ├── screen.py        ← 备用屏幕、光标控制
    ├── transcript.py    ← 可滚动对话区
    └── types.py         ← TranscriptEntry 等类型
```

---

## 核心数据流

```
用户输入
  │
  ▼
tty_app.py  ──→  agent_loop.py  ──→  anthropic_adapter.py  ──→  API
  │                    │                        │
  │ 渲染结果           │ 处理工具调用            │ 返回 AgentStep
  │                    │                        │
  ▼                    ▼                        ▼
tui/ 层             tooling.py              config.py
(渲染 ANSI)         (注册表/执行)           (模型/密钥)
```

### 一次对话的完整流程

1. **用户输入** → `tty_app.py` 解析按键
2. **构建消息** → 加入历史、系统提示（`prompt.py`）
3. **调用模型** → `agent_loop.py` 调 `model.next(messages)`
4. **处理响应**：
   - 如果是文本 → 追加到 transcript
   - 如果是工具调用 → `_execute_calls_in_order()`
     - 只读工具（`concurrency_safe`）并行执行
     - 写入/执行类工具独占串行
5. **返回结果** → `tty_app.py` 更新渲染
6. **循环** → 直到 `stop_reason` 或达到 max steps

---

## 关键设计决策

| 决策 | 说明 |
|------|------|
| **零外部依赖** | 纯标准库（`urllib`、`curses`、`subprocess`），无需 pip install |
| **同步模型** | 全程同步（非 async），CLI 工具场景够用 |
| **Zustand 风格 Store** | `state.py` 用 Updater 函数做不可变更新 + 订阅者通知 |
| **声明式工具** | 每个工具是完整 `ToolDefinition` 对象：schema + validator + run |
| **三级记忆** | 用户级（全局）、项目级（`.pepsi-code-memory/`）、本地级 |
| **MCP 集成** | 通过 stdio 子进程连接外部 MCP 服务器，自动注册工具 |

---

## 如何运行测试

```bash
pip install -e ".[dev]"
pytest                          # 全部测试
pytest tests/test_tools.py -v   # 特定模块
```

---

## 如何添加新工具

1. 在 `pepsicode/tools/` 创建 `my_tool.py`
2. 定义输入 schema（JSON Schema 格式）
3. 实现 `run(input, context) → ToolResult` 函数
4. 在 `tools/__init__.py` 注册

```python
# 最小示例
from pepsicode.tooling import ToolDefinition, ToolResult, ToolContext

MY_TOOL = ToolDefinition(
    name="my_tool",
    description="Does something useful",
    input_schema={
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"]
    },
    concurrency_safe=True,  # 只读工具设为 True
)
def run_my_tool(input_data, context: ToolContext) -> ToolResult:
    return ToolResult(ok=True, output=f"Got: {input_data['text']}")
MY_TOOL.set_run(run_my_tool)
```

---

## 常用命令

```bash
# Mock 模式（无需 API key）
PEPSI_CODE_MODEL_MODE=mock python -m pepsicode.main

# 恢复上次会话
python -m pepsicode.main --resume

# 列出所有会话
python -m pepsicode.main --list-sessions

# 配置诊断
python -m pepsicode.main --validate-config
```

---

## 进一步阅读

| 文档 | 内容 |
|------|------|
| `docs/reports/FINAL_AUDIT_REPORT.md` | 完整代码审计（功能/架构/安全/性能评分） |
| `docs/reports/PERFORMANCE_OPTIMIZATION_REPORT.md` | 性能优化细节（8 倍 token 估算提升等） |
| `docs/reports/CORECODER_OPTIMIZATION_REPORT.md` | 架构对齐优化（并行执行、上下文压缩等） |
| `docs/CONTRIBUTING.md` | Git 提交规范 |
