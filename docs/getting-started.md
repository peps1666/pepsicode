# 安装与启动

> 从源码安装、配置并启动 pepsicode。

[← 返回 README](../README.md)

## 快速开始

### 环境要求

- Python 3.11+
- Windows、macOS 或 Linux
- 一个 Anthropic 兼容接口；也可以使用内置 mock 模型进行本地验证

### 安装与启动

```bash
git clone https://github.com/peps1666/pepsicode.git
cd pepsicode
python -m pip install -e ".[dev]"
python -m pepsicode.main --install
```

安装后可以直接运行：

```bash
pepsicode
```

也可以不安装命令入口：

```bash
python -m pepsicode.main
```

使用 mock 模型启动：

```powershell
$env:PEPSI_CODE_MODEL_MODE = "mock"
python -m pepsicode.main
```

```bash
PEPSI_CODE_MODEL_MODE=mock python -m pepsicode.main
```

检查配置是否正确：

```bash
python -m pepsicode.main --validate-config
```
