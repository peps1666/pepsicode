# CoreCoder 架构对齐优化报告

> 参考 `CoreCoder-main` 的 7 篇 Claude Code 架构文章，对 pepsicode（MiniCode Python）
> 进行的一轮核心架构优化。目标：**更低的感知延迟、更省 token、更强韧性、可用的子 Agent 委派**，
> 同时保持「零运行时依赖」。

**结果**：全部 4 个方向落地并通过测试 —— `166 passed, 2 skipped`（原 144 + 新增 22 项针对性测试）。
另有一轮对抗式代码审查，确认并修复了实现过程中引入的 6 个真实 bug。

---

## 背景

pepsicode 是一个仿 Claude Code 的终端编程 Agent。与参考蓝本 `CoreCoder`（用 ~1400 行 Python
提炼 Claude Code 51 万行 TS 的核心模式）逐条比对后，发现它已实现了不少模式（唯一匹配 search-replace
编辑、危险命令分级 + 多级权限、会话持久化、MCP/skills、退避/抖动重试、`<progress>/<final>` 循环），
但在 4 个高价值维度上落后于 CoreCoder 的最小实现。本次优化修复这些差距。

---

## 实现的优化

### 1. 并行工具执行（文章 2 / 5）

**问题**：适配器全程阻塞，工具在 `agent_loop` 中串行逐个执行。`ToolCapability.CONCURRENCY_SAFE`
标志虽已定义却是死代码，从未接到任何工具上。连 CoreCoder 最小版都用了 ThreadPool。

**改动**：
- `tooling.py`：给 `ToolDefinition` 增加 `concurrency_safe: bool = False` 字段。
- 把 7 个只读工具标记为安全：`read_file`、`grep_files`、`list_files`、`file_tree`、
  `find_symbols`、`find_references`、`get_ast_info`。
- `agent_loop._execute_calls_in_order`：用 `ThreadPoolExecutor`（上限 8）并行执行**连续的**
  只读工具批次；写/执行类工具仍独占串行。
  **结果严格按原始调用顺序返回**，消息历史保持确定性。

**收益**：一次返回多个 `read_file`/`grep` 时，从「串行累加」变为「并行 ≈ max(单个)」。

---

### 2. HISTORY_SNIP 工具输出截断（文章 4 第 1 层）

**问题**：`grep_files`/`run_command` 把完整输出灌进上下文；`max_result_size_chars`
定义了却从不生效。这是最廉价、最高价值的压缩层（无需 LLM 调用）。

**改动**：
- `agent_loop._snip_tool_outputs`：每次模型调用前，裁短旧的大块 `tool_result`
  （保留头 12 行 + 尾 8 行，中间替换为 `[snipped N lines]`）。
- 保护最近 3 条结果（模型需要看到刚做了什么）、幂等（已截断的不再处理）。
- **绝不丢弃整条结果**（只缩短 content，保证 tool_use/tool_result 配对不被破坏）。

---

### 3. 上下文压缩 + 真实 token 用量（文章 4 第 2/3 层）

**问题**：`compact_messages` 只会「丢弃」最旧消息，无 LLM 摘要，长对话会丢失文件路径/决策/
未解决错误。上下文统计用 `chars/4` 启发式，丢弃了 API 已返回的真实 `usage`。

**改动**：
- `anthropic_adapter`：捕获 API 返回的真实 `usage`（`last_usage`），新增 `summarize()` 方法
  （用 fallback 或主模型把旧对话压成关键事实）。
- `context_manager`：
  - `update_usage()` + `get_stats()` 优先使用真实 input token 数。
  - `compact_messages` 增加 LLM 摘要路径（`summarizer` 回调），保留**文件路径 / 工具使用 /
    决策 / 未解决错误**；无模型时回退到启发式 `_heuristic_summary`。
- `main.py` / `tty_app.py`：把 `model.summarize` 接到 ContextManager 的 `summarizer`，
  并在 TTY 主交互路径也串联了 ContextManager（此前仅非交互路径有）。

---

### 4. API 韧性：溢出重试 + 回退模型（文章 2 细节 3）

**问题**：遇 400/413「请求过大」直接返回致命错误而非压缩重试；529 过载时无 fallback 模型。

**改动**：
- `anthropic_adapter`：新增 `ContextOverflowError` / `ServiceOverloadError`。
  - 400/413 → 抛 `ContextOverflowError`。
  - 529 → 切换 `fallbackModel`（若配置），仍失败才抛 `ServiceOverloadError`。
- `agent_loop`：捕获 `ContextOverflowError` → 强制压缩并重试（最多 3 次），而非退出本轮。
- `config.py`：新增 `fallbackModel` 配置项（环境变量 `ANTHROPIC_FALLBACK_MODEL` /
  `PEPSI_CODE_FALLBACK_MODEL` / settings.json `fallbackModel`）。

---

### 额外：系统提示 + 子 Agent

**治理规则块改为 opt-in**（文章 2 细节 1）：
- 此前每次请求都强制注入 ~70 行「Engineering Governance Rules」（指向外部路径、含非法 Python 示例），
  跨项目不合适且每轮耗 token。
- 现改为可选：`governance` 配置项或 `PEPSI_CODE_GOVERNANCE=1` 才启用。
- 系统提示新增**动态环境块**：OS、Python 版本、CWD、git 分支、顶层目录条目。

**子 Agent 暴露为 `task` 工具**（文章 6）：
- `sub_agents.py` 中的 Explore/Plan/General 此前只在测试里被引用，模型无法调用。
- 新增 `tools/task.py`：`task` 工具，模型可委派隔离上下文的子任务，只回传摘要。
- 子 Agent 用受限工具集（只读 / general 含写执行），**禁止递归**（子注册表不含 task 工具本身）。

---

## 对抗式审查发现并修复的 6 个真实 bug

实现后用一个 review → verify 工作流对改动做对抗式复查，确认并全部修复：

| 严重度 | 问题 | 修复 |
|---|---|---|
| 高 | `read_file._file_cache` 并发下的字典竞态（迭代时被修改 / 重读 del KeyError） | 加 `threading.Lock` 保护读取/清理/写入 |
| 高 | `compact_messages` 不重置陈旧 `actual_input_tokens`，压缩后仍判定超限 | 压缩后 `actual_input_tokens = 0` |
| 中 | 529 在 `_send` 内被当普通 5xx 重试 5 次才走 fallback | `_should_retry_status` 排除 529，交给 `next()` |
| 中 | 位置配对导致 `tool_result` 成孤儿 → 触发远端 400 | 改为按 `toolUseId` 配对，修复孤儿 |
| 低 | 空操作压缩仍插入空 marker、`messages_removed` 为负 | 无变更时原样返回、计数 clamp |
| 低 | 压缩 marker 跨轮累积成假 system prompt | marker 单独标记，不混入真实 system prompt |

---

## 涉及的文件

**核心**
- `pepsicode/agent_loop.py` — 并行执行、HISTORY_SNIP、溢出重试、usage 捕获
- `pepsicode/anthropic_adapter.py` — 溢出/过载异常、fallback 模型、`summarize()`、usage
- `pepsicode/context_manager.py` — 真实 token、LLM/启发式摘要、配对修复、marker 治理
- `pepsicode/config.py` — `fallbackModel`、`governance` 配置项
- `pepsicode/prompt.py` — 治理块 opt-in、动态环境块
- `pepsicode/tooling.py` — `concurrency_safe` 字段
- `pepsicode/tools/task.py` — 新增子 Agent 委派工具

**工具标记**：`read_file.py`、`grep_files.py`、`list_files.py`、`file_tree.py`、`code_nav.py`

**入口串联**：`pepsicode/main.py`、`pepsicode/tty_app.py`、`pepsicode/tools/__init__.py`

**测试**：`tests/test_optimizations.py`（新增 22 项）

---

## 验证

```bash
cd pepsicode
python -m pytest -q          # 166 passed, 2 skipped
```

新增测试覆盖：并行执行顺序保持、HISTORY_SNIP 幂等与保护、溢出压缩重试、真实 token 覆盖、
LLM/启发式摘要、治理 opt-in、Task 工具、以及 6 个 bug 的回归测试。

---

## 配置示例

`~/.pepsi-code/settings.json`：

```json
{
  "model": "claude-opus-4-20250514",
  "fallbackModel": "claude-sonnet-4-20250514",
  "governance": false,
  "env": {
    "ANTHROPIC_BASE_URL": "https://api.anthropic.com",
    "ANTHROPIC_API_KEY": "<your-key>"
  }
}
```
