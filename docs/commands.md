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
| `/image <路径> [问题]` | 终端 TUI 发送本地 PNG、JPEG、GIF 或 WebP 图片；路径有空格时加引号，最大 5 MB |
| `/paste-image` | 终端 TUI 从 Windows 剪贴板附加图片；可用 `Ctrl+V` 或 `Alt+V`，然后输入问题并按 Enter。若终端拦截 `Ctrl+V`，使用 `Alt+V` 或本命令 |

粘贴本地图片文件路径时，TUI 会读取文件并附加图片数据，不把路径当作给模型的文字。附加后输入区显示 `[图片 ×数量]`，不显示缩略图。按 `Ctrl+G` 可删除最近附加的一张图片并保留已输入的文字；输入光标在开头时也可按 Backspace 删除图片。`Esc` 会清空整条草稿及所有图片。模型必须支持图片输入；DeepSeek 的 `deepseek-v4-pro` 会丢弃图片，请切换到 `deepseek-flash`。
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

终端输入 `/tree` 或 `/fork` 可按消息内容选择对话位置；`/resume` 显示当前工作区的历史对话，以第一条用户消息作为主题，并显示最近更新时间，选择后即可继续，无需输入 ID。没有实际内容的新会话不保存，旧空会话从列表隐藏但不删除；未完成的执行、计划和历史分支仍会保留。`/permissions default` 按选项设置工作区新会话的默认权限。`/hooks` 提供操作与 Hook 选择，`/worktree` 提供操作、工作区和退出方式选择。使用方向键、Enter 和 Esc 操作；创建 worktree 的新名称等自由文本仍需输入。带参数的命令仍可用于脚本和熟悉命令的用户。

QQ 桥接为断线重连保留必要的会话状态；未产生对话内容时，这种后台状态不会显示在历史列表中。
