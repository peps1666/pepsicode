# pepsicode 快速上手指南

## 这是什么？

pepsicode 是本地编程代理，终端 TUI 和 Electron 桌面共用代理内核。当前源码为 2.0.7 开发候选；版本号不表示该候选已完成正式发布或成品验收。

## 从源码启动

需要 Python 3.11 或更新版本，以及项目源码。在仓库根目录执行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe -m pepsicode --version
.\.venv\Scripts\python.exe -m pepsicode --validate-config
.\.venv\Scripts\python.exe -m pepsicode
```

macOS/Linux 使用 `.venv/bin/python`。必需依赖以 [pyproject.toml](pyproject.toml) 为准，目前包括 `psycopg2-binary` 和 `websockets`，安装时由 pip 解析；不是零依赖项目。开发测试使用 `pip install -e ".[dev]"`；可选语言服务使用 `pip install -e ".[intelligence]"`。

配置通常放在用户目录的 `.pepsi-code/settings.json`；支持的项目设置与环境变量优先级见 [模型配置](docs/configuration.md#模型配置)。推荐通过设置界面填写服务商、模型与 API Key；配置诊断仅输出是否已配置及来源，避免把真实凭据写进示例或报告。

离线检查可使用 mock 模式，不调用付费接口：

```powershell
$env:PEPSI_CODE_MODEL_MODE = "mock"
python -m pepsicode --validate-config
python -m pepsicode.evals --mode smoke --output .evals/first-smoke
```

`--output` 必须是尚不存在的目录。正式连接模型前清除 mock 环境变量，并检查服务商配置与请求/金额上限。使用测试用户目录验证时，同时设置 `USERPROFILE` 和 `HOME` 指向专用临时目录。

未配置服务商时，`--validate-config` 报告缺少模型或鉴权是预期诊断；mock 模式不会把它变成真实配置已就绪。离线 smoke 不需要这些凭据。

## 使用 Windows ZIP

下载与目标发行版本匹配的 ZIP 和校验值，完整解压后运行 `Pepsicode.exe`。便携包内置 Electron/Node 运行时及冻结 Python 后端，启动应用无需系统 Python/Node；不要单独移动 EXE，`resources` 等同级文件必须保留。源码候选的测试不能替代某个下载成品的验收。

便携包不包含用户项目的所有开发工具。代理要运行项目的 Python、pytest、Node、编译器或语言服务时，仍需项目虚拟环境或已安装的工具链。只读和 Plan 权限继续限制执行，应用层审批不等于操作系统隔离。

## 配置与启动故障

- 源码启动缺包：用启动时的同一个 Python 执行 `-m pip install -e .`，核对虚拟环境。
- 桌面提示缺少内置后端：重新完整解压对应 ZIP；源码开发先按 [Windows 构建说明](docs/windows-zip-tdd.md) 构建后端。
- 连接或鉴权错误：运行 `--validate-config`，核对服务商地址、模型和凭据来源，勿直接发送配置全文。
- 检查被拒绝、用量未知或任务未完成：查看具体原因及 [任务验收说明](docs/task-orchestration.md)，不要把进程退出当成行为已验证。

开发构建身份、成品验证与剩余限制见 [项目问题清单](docs/project-issues.md) 和 [执行边界](docs/harness-boundaries.md)。

## QQ 私聊入口

想把 Pepsi 加为 QQ 好友并通过私聊使用，可按 [QQ 接入指南](integrations/qq/README.md) 配置独立消息通道。
该入口需要已登录的 OneBot v11 网关，首次使用先配置模型、你的 QQ 号和网关令牌。
