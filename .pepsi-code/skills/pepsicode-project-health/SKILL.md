---
name: pepsicode-project-health
description: 检查 Pepsicode 仓库的项目元数据、Skill/MCP 接入状态和基础质量；当用户要求项目体检、验证扩展接入或运行基础检查时使用。
---

# Pepsicode Project Health

输出一份有证据的项目健康报告，不主动修改代码或安装依赖。

1. 如果 `mcp__workspace-inspector__project_info` 可用，先调用它获取项目名称、版本、Python 文件数、测试文件数、已发现的项目 Skill 和 MCP 配置；不可用时再直接读取仓库文件。
2. 查看 Git 工作树状态，区分用户已有改动和本次检查产生的改动。
3. 运行 `python -m ruff check pepsicode tests`。
4. 运行与当前请求直接相关的定向测试；只有用户要求完整验证时才运行全量测试。
5. 前端依赖已安装时运行 `npm run build`；缺少 `node_modules` 时只报告准备命令，不自动安装。
6. 先给结论，再列出通过项、失败项、未验证项和建议的下一步。不要把环境缺失误报成代码缺陷。
