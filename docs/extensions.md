# 扩展与自动化

> Markdown 自定义命令、Skills 与 MCP。

[← 返回 README](../README.md)

## Markdown 自定义命令

将 Markdown 文件放到当前项目的 `.pepsi-code/commands/`，或个人目录 `~/.pepsi-code/commands/`，文件名就是命令名。同名时项目版本优先；内置命令和 `/queue` 为保留名称，不能覆盖。第一版只读取目录中的直接 `.md` 文件，名称使用 1–64 个英文字母、数字、下划线或连字符，首字符必须是字母或数字。

本仓库已提供 `/review` 和 `/fix-test` 两个项目命令。也可以创建 `.pepsi-code/commands/explain.md`：

```markdown
---
description: 解释模块设计和调用关系
argument-hint: "[文件路径] [关注点]"
---
解释 ${1:-当前项目} 的设计和调用关系。
重点关注 ${2:-模块职责与数据流}，给出具体代码位置。
```

调用示例：`/explain "src/main.py" "错误处理"`。在终端或桌面输入 `/` 可浏览命令；Tab 补全命令名，参数提示不会作为文字插入。桌面支持上下键选择、鼠标点击和刷新列表。输入 `/commands` 查看来源和加载错误，`/commands reload` 强制重载；菜单会短暂缓存，提交命令时始终重新读取文件，因此修改模板无需重启。

| 写法 | 展开结果 |
| --- | --- |
| `$1`、`$2`… | 对应位置参数，缺失时为空 |
| `$ARGUMENTS` 或 `$@` | 所有参数，用空格连接 |
| `${1:-默认值}`、`${@:-默认值}` | 参数缺失或为空时使用默认值 |
| `$$` | 字面量 `$`，例如 `$$1` 得到 `$1` |

单引号和双引号可组合带空格的参数，反斜杠按字面量保留，适合 Windows 路径；引号不闭合会报错。参数只替换一次，不展开环境变量、shell 命令或嵌套表达式。仅解析 `description` 和 `argument-hint` 两个元数据字段；`allowed-tools` 等字段不会授予权限。模板展开为普通用户消息，不改变系统提示词、权限模式或工具审批。

自定义命令可以作为补充指令或后续任务排队。加入队列时固定展开内容；之后修改模板不会改变已经排队的任务。聊天记录显示原始调用，持久会话同时保存展开内容和模板路径；取回或停止时退回原始命令，方便修改参数。

空模板、无效编码、保留名称和路径越界会显示加载错误；项目版本损坏时不会静默退回同名个人版本。每个模板最多 64 KiB，每次最多扫描 256 个文件，参数最多 32000 字符，展开结果最多 128000 字符；排队输入还受队列的 32000 字符限制。

## Skills 与 MCP

仓库内置了一个可以直接验证扩展链路的示例：

- `.pepsi-code/skills/pepsicode-project-health/SKILL.md`：项目健康检查 Skill。
- `examples/mcp/workspace_inspector_server.py`：零外部依赖、只读的 stdio MCP 服务。
- `.mcp.json` 中的 `workspace-inspector`：项目级 MCP 连接配置。
- `.mcp.json` 中的 `sequential-thinking`：本地 stdio 思考链服务。
- `.mcp.json` 中的 `openai-developer-docs`：OpenAI 官方公开、只读的远程 Streamable HTTP MCP。

启动 Pepsicode 后运行 `/skills` 和 `/mcp` 应看到 `pepsicode-project-health`、本地 `workspace-inspector` 和远程 `openai-developer-docs`。在对话中要求“使用 pepsicode-project-health 检查项目”，Skill 会优先调用本地 MCP 的 `project_info` 工具获取仓库元数据；询问 OpenAI API、Codex 或插件问题时，可以调用远程文档 MCP 搜索官方资料。

`.mcp.json` 支持为每个服务器声明 `readOnlyTools`（数组或 `"*"`）：声明的工具在自动模式下免确认，未声明的按需审批。

也可以不启动模型，直接运行定向集成测试：

```bash
python -m pytest -q tests/test_project_extensions.py
```
