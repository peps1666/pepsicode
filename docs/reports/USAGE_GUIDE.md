# pepsicode Python - 使用指南

> 版本: v0.2.0
> 更新时间: 2026-04-05

---

## 🚀 快速开始

### 1. 安装（首次使用）

```bash
# 运行交互式安装向导
python -m pepsicode.main --install
```

安装向导会要求输入：
- **Model name**: 模型名称（如 `claude-sonnet-4-20250514`）
- **ANTHROPIC_BASE_URL**: API 地址（默认 `https://api.anthropic.com`）
- **ANTHROPIC_AUTH_TOKEN**: API 密钥

配置会保存到 `~/.pepsi-code/settings.json`

### 2. 启动

```bash
# 正常启动
python -m pepsicode.main

# 或使用 mock 模式（无需 API，用于测试）
set PEPSI_CODE_MODEL_MODE=mock
python -m pepsicode.main
```

### 3. 基本使用

启动后你会看到全屏 TUI 界面，包含 Banner、会话区、输入提示和 Footer。

**输入你的问题**，然后按 Enter。Mock 模式下会模拟 AI 响应。

---

## 📋 命令行选项

### 会话管理

```bash
# 列出所有保存的会话
python -m pepsicode.main --list-sessions

# 恢复最近的会话
python -m pepsicode.main --resume

# 恢复特定会话
python -m pepsicode.main --resume <session_id>
```

---

## ⌨️ Slash 命令

| 命令 | 说明 |
|------|------|
| `/help` | 显示帮助信息 |
| `/tools` | 列出所有可用工具 |
| `/cost` | 显示 API 费用 |
| `/config` | 配置诊断 |
| `/context` | 上下文使用率 |
| `/memory` | 记忆系统状态 |
| `/exit` | 退出 |

---

## 🔧 配置

编辑 `~/.pepsi-code/settings.json`：

```json
{
  "model": "deepseek-v4-flash",
  "env": {
    "ANTHROPIC_BASE_URL": "https://api.deepseek.com/anthropic",
    "ANTHROPIC_API_KEY": "your-key"
  }
}
```

### 环境变量

| 变量 | 说明 |
|------|------|
| `ANTHROPIC_API_KEY` | API 密钥 |
| `ANTHROPIC_BASE_URL` | API 地址 |
| `ANTHROPIC_MODEL` | 模型名称 |
| `PEPSI_CODE_MODEL_MODE` | 设为 `mock` 测试模式 |
