# Git 提交规范

## 提交信息格式

采用 [Conventional Commits](https://www.conventionalcommits.org/) 规范：

```
<type>(<scope>): <subject>

<body>

<footer>
```

### 1. type（必填）

| type | 说明 | 示例 |
|------|------|------|
| `feat` | 新功能 | `feat: 添加 MCP 服务器管理` |
| `fix` | 修复 bug | `fix: 修复中文锚点链接无效` |
| `docs` | 文档修改 | `docs: 更新 README 安装说明` |
| `style` | 格式调整（空格、缩进，不影响逻辑） | `style: 统一引号为双引号` |
| `refactor` | 重构（不增功能也不修 bug） | `refactor: 提取公共权限校验` |
| `perf` | 性能优化 | `perf: 优化 token 估算算法` |
| `test` | 测试相关 | `test: 补充权限模块单元测试` |
| `chore` | 构建/工具/依赖 | `chore: 升级 pytest 到 8.0` |
| `ci` | CI/CD 配置 | `ci: 添加 GitHub Actions` |
| `revert` | 回滚 | `revert: 回滚 feat: 添加 MCP 管理` |

### 2. scope（可选）

影响范围，用括号包裹。本项目建议：

| scope | 范围 |
|-------|------|
| `agent` | agent_loop, sub_agents |
| `tools` | tools/ 目录 |
| `tui` | tui/ 目录 |
| `config` | 配置相关 |
| `mcp` | MCP 集成 |
| `session` | 会话管理 |
| `memory` | 记忆系统 |
| `docs` | 文档 |

### 3. subject（必填）

- 中文或英文，保持统一即可
- 不超过 50 个字符
- 不加句号结尾
- 用祈使句：「添加」而非「添加了」

### 4. body（可选）

详细描述，每行不超过 72 字符。说明**为什么**改、**怎么**改。

### 5. footer（可选）

- `BREAKING CHANGE:` 不兼容变更
- `Closes #123` 关联 issue

---

## 示例

### 简单提交

```bash
git commit -m "docs: 修复 README 锚点链接"
git commit -m "feat(tools): 添加 web_search 工具"
git commit -m "fix: 修复 Windows 下路径分隔符错误"
```

### 详细提交

```bash
git commit -m "feat(mcp): 支持多 MCP 服务器同时连接

支持同时连接多个 MCP 服务器，命名空间隔离工具名，
避免冲突。通过 .mcp.json 配置多个 server 条目。

Closes #42"
```

### 不兼容变更

```bash
git commit -m "refactor(config): 重命名配置键 maxTokens → maxOutputTokens

BREAKING CHANGE: settings.json 中 maxTokens 改为 maxOutputTokens，
旧配置键不再兼容。"
```

---

## 分支命名

| 分支 | 用途 |
|------|------|
| `master` / `main` | 主分支，可发布状态 |
| `feat/<功能>` | 新功能：`feat/mcp-server` |
| `fix/<问题>` | 修复：`fix/chinese-encoding` |
| `docs/<内容>` | 文档：`docs/api-reference` |
| `refactor/<模块>` | 重构：`refactor/tool-registry` |

---

## 提交频率

✅ **推荐**：一个小功能 / 一个修复 → 一次提交

- 每完成一个独立改动就提交
- 提交信息精确描述改了什么
- 方便以后 `git log` 查看和 `git revert` 回滚

❌ **避免**：

- 攒了几天的改动一次提交
- 提交信息写「修改了一堆东西」「更新」
- 把不相关的改动混在一次提交里

---

## 提交前检查清单

- [ ] 代码能跑通（`python -m pepsicode.main`）
- [ ] 测试通过（`pytest`）
- [ ] 没有遗留调试代码（`print`、`breakpoint()`）
- [ ] 敏感信息已排除（密钥、本地路径）
- [ ] `.gitignore` 覆盖新增的生成文件
- [ ] 提交信息清晰描述了改动

---

## 常用命令速查

```bash
# 查看状态
git status

# 查看改动详情
git diff

# 添加单个文件
git add README.md

# 添加所有改动
git add .

# 提交
git commit -m "docs: 更新提交规范文档"

# 查看提交历史
git log --oneline -10

# 修改最近一次提交信息
git commit --amend -m "新的提交信息"

# 推送到 GitHub
git push

# 撤销未提交的改动
git checkout -- README.md          # 单个文件
git checkout -- .                  # 所有文件
```
