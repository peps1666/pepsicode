# Hooks v2

> 生命周期事件、动作、条件模板、管理命令与 Python 兼容 API。

[← 返回 README](../README.md)

## 配置层级

按以下顺序加载，同 ID 的可信后层配置覆盖前层配置：

1. `~/.pepsi-code/hooks.json`：用户级，自动视为可信。
2. `<workspace>/.pepsi-code/hooks.json`：项目级，适合提交到仓库。
3. `<workspace>/.pepsi-code/hooks.local.json`：本机覆盖，默认已加入 `.gitignore`。

项目级和本机 Hook 默认不执行。首次发现时，交互模式会提供“信任当前版本”“仅本次信任”“保持禁用”三个选择；非交互模式保持禁用。永久信任记录绑定到路径和文件内容 SHA-256 指纹，配置一旦变化就必须重新确认。

## 最小配置

```json
{
  "version": 1,
  "hooks": [
    {
      "id": "protect-secrets",
      "event": "pre_tool_use",
      "priority": 10,
      "when": {
        "tool": ["write_file", "edit_file", "patch_file"],
        "args.path": {"regex": "(^|[/\\\\])\\.env($|[/\\\\])"}
      },
      "action": {
        "type": "deny",
        "message": "拒绝修改受保护的环境配置：{path}"
      }
    },
    {
      "id": "review-after-tests",
      "event": "post_tool_use",
      "when": {
        "tool": "run_command",
        "args.command": {"contains": "pytest"}
      },
      "action": {
        "type": "context",
        "message": "测试命令已结束。检查失败原因和回归风险后再继续。"
      }
    },
    {
      "id": "notify-session-save",
      "event": "session_save",
      "action": {
        "type": "notify",
        "message": "会话 {session_id} 已保存"
      }
    }
  ]
}
```

配置根节点也可直接写成 Hook 数组。设置 `"strict": true` 后，只要同一文件存在一条无效配置，整份文件都不会加载。

## 支持的事件

| 事件 | 触发时机 |
| --- | --- |
| `pre_tool_use` | 工具校验后、权限检查和实际执行前 |
| `post_tool_use` | 工具执行完成后 |
| `agent_start` / `agent_stop` | 主代理回合开始和结束 |
| `subagent_start` / `subagent_stop` | 子代理开始和结束 |
| `user_input` | 收到用户输入 |
| `assistant_output` | 代理产出最终回复 |
| `context_compact` | 自动或溢出恢复压缩完成 |
| `session_save` / `session_resume` | 会话保存和恢复 |
| `startup` / `shutdown` | 应用启动和关闭 |
| `error` | 工具或模型流程发生错误 |

## 动作

`deny`

- 仅允许用于 `pre_tool_use`。
- 在权限检查和工具执行前同步阻断，并向模型返回明确错误。

`notify`

- 向 CLI/TTY 输出通知，不修改模型上下文。

`context`

- 将受长度限制的 `<hook-context>` 块加入下一次模型调用。
- 注入内容不会追加到会话消息，因此不会在多轮中重复积累。

`command`

- 使用 argv 数组，不通过字符串 shell 拼接。
- 复用 `run_command` 的目录边界、命令分类和权限审批。
- 不能用于 `pre_tool_use`，Plan 模式中始终禁用。
- 支持 `timeoutSeconds`、`background` 和 `exposeOutputToModel`。
- 大输出会写入 `.pepsi-code/tool-results/`，模型和 UI 只接收有界预览。

示例：

```json
{
  "id": "format-after-write",
  "event": "post_tool_use",
  "scope": ["main"],
  "when": {
    "tool": ["write_file", "edit_file", "patch_file"],
    "args.path": {"glob": "*.py"}
  },
  "action": {
    "type": "command",
    "argv": ["python", "-m", "ruff", "format", "{path}"],
    "timeoutSeconds": 30,
    "exposeOutputToModel": true
  }
}
```

命令是否获准仍由当前 PermissionManager 决定。Hook 配置本身不能声明“免审批”。

## 条件与模板

`when` 支持以下结构化操作符：

- 直接值：`eq`
- 数组：`in`
- `{ "contains": "..." }`
- `{ "glob": "..." }`
- `{ "regex": "..." }`

常用字段包括 `event`、`tool`、`path`、`args.<name>`、`result.<name>`、`permission_mode`、`agent_scope` 和错误字段。正则表达式和参与匹配的值都有长度限制，避免配置造成无界处理。

消息与 argv 支持 `{event}`、`{tool}`、`{path}`、`{cwd}`、`{permission_mode}`、`{agent_scope}`、`{error}` 和 `{args.<name>}` 替换。

## Scope、顺序与一次性执行

- `scope` 可为 `main`、`subagent` 或两者数组，默认两者都执行。
- `priority` 越小越先执行，默认 `100`。
- `once: true` 在单进程中最多执行一次；并行工具调用下也使用原子 claim。
- `background: true` 只适用于非前置动作，后台任务由受管线程池执行。
- Hook 失败不会让代理循环崩溃；`onError` 可设为 `ignore`、`warn`，前置 Hook 还可用 `deny`。

## 管理命令

```text
/hooks
/hooks list
/hooks errors
/hooks reload
/hooks trust
/hooks enable <id>
/hooks disable <id>
```

`/hooks enable` 和 `/hooks disable` 只影响当前进程。`/hooks trust` 会为当前项目 Hook 文件保存内容指纹，并重新加载配置。

## Python 兼容 API

旧版程序化事件 API 保持可用：

```python
from pepsicode.hooks import HookEvent, register_hook

unregister = register_hook(
    HookEvent.POST_TOOL_USE,
    lambda context: print(context.tool_name),
    "audit tool usage",
)
```

`HookManager`、`register_hook`、`fire_hook`、`fire_hook_sync`、`create_logging_hook` 和 `create_script_hook` 均保留。配置驱动的新 Hook 建议优先使用 `hooks.json`，因为它具备验证、信任和权限治理。
