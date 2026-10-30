# 命令速查

> 终端与桌面共用的常用斜杠命令。

[← 返回 README](../README.md)

## 常用命令

| 命令 | 用途 |
| --- | --- |
| `/help` | 查看命令帮助 |
| `/model [list\|refresh\|模型 ID] [--save]` | 查看、刷新或切换模型；可选保存默认设置 |
| `/provider [服务商 ID] [模型 ID] [--save]` | 选择服务商与模型；`list` 查看配置状态 |
| `/effort [档位] [--save]` | 查看或调整当前模型的思考深度 |
| `/permissions [模式]` | 查看或切换权限模式；`default <模式>` 设置当前工作区的新会话默认 |
| `/plan [task]` | 进入只读 Plan 模式 |
| `/hooks` | 查看 Hooks v2 状态、信任、启停 |
| `/tools` | 列出工具 |
| `/skills` | 列出 Skills |
| `/mcp` | 查看 MCP 状态 |
| `/commands` | 查看自定义命令来源与加载错误 |
| `/tree`、`/fork`、`/clone`、`/retry` | 对话分支与重新生成 |
| `/queue [任务]` | 查看队列或排入后续任务 |
| `/context` | 查看上下文占用 |
| `/cost`、`/cost-report` | 查看模型调用成本 |
| `/tasks` | 查看后台任务 |
| `/mem`、`/memory` | 查看持久记忆 |
| `/resume [id]` | 恢复会话 |
| `/worktree` | 管理 Git worktree |
| `/exit` | 退出并保存会话 |

工具快捷命令可绕过模型直接执行：`/ls [目录]`、`/grep 模式 :: 路径`、`/read 文件`、`/write 目标 :: 内容`、`/edit 目标 :: 旧 :: 新`、`/patch 目标 :: 旧 :: 新`、`/modify 目标 :: 内容`、`/cmd 命令`。

终端 TUI 空闲时按 `Tab` 切换 Plan / Build；斜杠命令补全优先。手动切回 Build 不会批准或自动执行计划，也不会改变原有权限模式。
