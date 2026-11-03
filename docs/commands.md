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
| `/skills` | 重新扫描并列出 Skills，无需重启 |
| `/skills install find-skills [--global]` | 安装 find-skills；省略 `--global` 时安装到当前项目 |
| `/skills install <owner/repo 或 GitHub URL> <skill> [--global]` | 从 GitHub 安装指定 Skill |
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

最短安装流程：输入 `/skills install find-skills`，完成后用 `/skills` 查看，再让助手使用 `find-skills`。默认仅安装到当前项目；需要所有项目共用时加 `--global`。也可输入 `/skills install https://github.com/vercel-labs/skills find-skills`。

安装命令直接输入 PepsiCode 的输入行，不要带 `$` 提示符或反引号。安装需要 Build 模式和允许命令审批的权限；若当前显示 Plan，先清空输入行，在终端空闲时按 `Tab` 切到 Build，再重新输入命令。切换模式不会自动安装，也不会提高权限；`read_only` 下还需通过 `/permissions` 选择合适的权限模式。Plan 下仍可使用 `/skills` 查看列表。

安装复用 [Vercel Skills CLI](https://github.com/vercel-labs/skills)，需要 Node.js（含 npm）、Git 和网络。它经过现有命令执行层审批，以非交互复制方式写入 `.claude/skills`（全局为 `~/.claude/skills`），无需 Windows 符号链接权限。TUI 和桌面端安装成功后立即刷新目录；通过其他终端安装后执行 `/skills` 即可刷新。安装前应确认来源可信。

PepsiCode 兼容项目和用户主目录中的 `.pepsi-code/skills`、`.claude/skills`、`.agents/skills`，每个 Skill 使用 `<name>/SKILL.md`。同名时保持原优先级：PepsiCode 项目、PepsiCode 用户、Claude 项目、Claude 用户，然后共享 Agents 项目和用户目录。

Windows 的命令执行层会自动解析 `npx.cmd`、`npm.cmd` 等批处理包装器，不需要手动添加 `cmd /c`。如果提示找不到 `npx`，安装 Node.js 后重新打开 PepsiCode，让进程获得新的 PATH。隐式批处理调用不接受引号或 Shell 控制字符参数；复杂 Shell 语法需显式指定 Shell，并经过同样审批。

终端 TUI 空闲时按 `Tab` 切换 Plan / Build；斜杠命令补全优先。手动切回 Build 不会批准或自动执行计划，也不会改变原有权限模式。

终端输入 `/tree` 或 `/fork` 可按消息内容选择对话位置；`/resume` 显示当前工作区的历史对话，以第一条用户消息作为主题，并显示最近更新时间，选择后即可继续，无需输入 ID。没有实际内容的新会话不保存，旧空会话从列表隐藏但不删除；未完成的执行、计划和历史分支仍会保留。`/permissions default` 按选项设置工作区新会话的默认权限。`/hooks` 提供操作与 Hook 选择，`/worktree` 提供操作、工作区和退出方式选择。使用方向键、Enter 和 Esc 操作；创建 worktree 的新名称等自由文本仍需输入。带参数的命令仍可用于脚本和熟悉命令的用户。

QQ 桥接为断线重连保留必要的会话状态；未产生对话内容时，这种后台状态不会显示在历史列表中。
