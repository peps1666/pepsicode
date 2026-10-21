# Windows ZIP 便携版：TDD 实施与验收

目标：生成 `pepsicode-client/release/Pepsicode-<版本>-win-x64.zip`。完整解压后双击 `Pepsicode.exe`，无需预装 Python / Node 即可启动界面与代理后端。

这里的便携指免安装。配置、会话继续保存在用户目录，更新时替换程序文件即可；用户项目自己的 Git、Node、Python、Docker、语言服务器仍由该项目的开发环境提供。

## 流程与验收

每步遵守「先写失败测试 → 实现 → 测试通过 → 更新记录」。

| 步骤 | 任务与先写的测试 | 完成标准 | 注意事项 | 状态 |
| --- | --- | --- | --- | --- |
| 1 | 后端启动配置：开发模式使用 Python；发布模式使用内置 EXE；含空格、中文的路径不拆分 | 单测通过；内置后端缺失时明确报错 | 发布版不得回退到系统 Python；不记录连接令牌 | 已完成 |
| 2 | 启动生命周期：等待自己启动的后端就绪；进程错误、提前退出、超时均失败；退出清理自己启动的进程 | 单测覆盖成功、失败、超时、清理 | 删除按端口批量杀 Python 的旧逻辑；支持重复启动聚焦已有窗口 | 已完成 |
| 3 | 冻结后端：PyInstaller 单目录模式，包含内置 agent Markdown；修正冻结后 `sys.executable` 的用途 | 后端从任意工作目录启动；认证、建会话、mock 对话成功 | 构建用独立虚拟环境；不打包本机 Key、设置、会话；项目测试不能误启动服务端 EXE | 已完成 |
| 4 | Electron ZIP：后端放入 `resources/backend`；前端使用相对资源 URL；附使用说明、许可证 | 一条构建命令产出 ZIP 和 SHA-256；缺少后端时构建失败 | 使用锁定依赖；后端放在 ASAR 外；ZIP 必须整体解压 | 已完成 |
| 5 | 成品验收：解压到含中文与空格的新目录；隔离用户数据；PATH 移除 Python / Node | 成品界面连接成功；mock 消息往返；关闭后后端退出 | 实测与干净 Windows 虚拟机验收分别记录，不把模拟 PATH 等同于真机验证 | 本机成品验收完成；纯净虚拟机待人工复核 |
| 6 | 可重复构建：GitHub Actions 手动构建并上传 artifact；更新 README 和发布清单 | CI 配置可用；本地测试、构建、成品验收结果记录 | 本次生成本地文件和工作流，不自动创建 Release 或上传公开资产 | 工作流、文档已完成；GitHub 远端尚未执行 |

## 初版范围

- 平台：Windows 10/11 x64，启动器使用系统自带的 .NET Framework CLR。
- 后端：PyInstaller 单目录；Electron 管理启动与退出。
- 分发：ZIP，无安装向导；首次使用在界面配置服务商与 API Key。
- 不包含：自动更新、代码签名、macOS / Linux 安装包、用户项目的开发工具链。

## 验证记录

- Red：初始 8 项后端启动测试在实现前失败；冻结运行时与归档测试在模块缺失时失败；打包门禁测试在模块缺失时失败；后续数据隔离用例先失败再实现。
- Green：客户端 50 项测试通过，覆盖启动、失败、超时、资源门禁、Windows 启动器参数转义与子进程退出码；Python 56 项相关回归测试通过（包括桌面运行时、ZIP、自动验证、Hook、服务端安全和协议）。
- `python scripts/build_desktop_backend.py`：成功；内置后端 `--self-test` 返回 `frozen: true`，四种内置 agent 均存在。
- `npm run release:zip`：成功，生成 ZIP、SHA-256；归档 CRC 校验通过。
- `build/desktop-venv/Scripts/python.exe scripts/smoke_desktop_zip.py pepsicode-client/release/Pepsicode-2.0.0-win-x64.zip`：通过，真实解压后端完成认证、创建会话和 mock 对话。
- `npm run release:smoke`：通过，从 ZIP 的实际入口启动隐藏桌面窗口，真实渲染器和内置后端完成消息往返，关闭后验证后端端口不再监听。结果文件：`build/desktop-zip-smoke.json`。
- 两项成品验收都使用中文与空格路径、隔离用户数据，并将运行进程的 PATH 限制为 Windows System32；测试没有替换 WebSocket、注入业务状态或使用真实 API Key。

## 验收发现与处理

1. 原生压缩器产生过 CRC 错误的中间 ZIP。发布流水线现使用标准 ZIP 归档器，并在原子替换产物前逐文件验证 CRC，防止把损坏包作为成品。
2. 当前 Windows 上兼容启动参数必须在 Electron 原生初始化前传入。因此根目录 `Pepsicode.exe` 是小型 Windows 启动器，启动同目录的 `PepsicodeDesktop.exe`，转发原有 Windows `--no-sandbox` 参数。启动器不用命令 shell，中文、引号、空参数、结尾反斜杠和退出码已实测。
3. 隔离数据通过 `PEPSI_CODE_DESKTOP_DATA_DIR` 完成：Electron 用户数据放在该目录，后端用户主目录放在其 `backend-home` 子目录，避免篡改 GUI 原生用户环境。默认使用方式不需要设置此变量。

## 发布前人工清单

- 在全新 Windows 10/11 x64 虚拟机中解压 ZIP，用正常可见窗口启动，配置测试服务商并完成真实模型请求。
- 核对随包说明与 SHA-256；完整保留两个 EXE、resources 和 DLL。
- 当前包未签名；公开分发前按发布策略考虑代码签名。本次没有配置自动更新。
- GitHub 手动工作流需要在远端单独触发；v2.0.0 已作为公开 Release 发布。

## v2.0.1：界面配置 Key 与终端小猫图标

1. Red：补充真实 React 组件交互测试，复现内置服务商无法进入 Key 配置的问题；补充服务端公开预设地址测试，初始失败。
2. Green：移除未配置服务商选项的禁用行为；选择未配置项进入三步配置向导（连接、模型、确认）；预填后端提供的内置地址，Key 使用密码框。已配置服务商可通过「配置服务商 / API Key」更新。
3. 回归：客户端 54 项和后端相关 61 项通过；组件验证四个内置服务商入口、确认前不保存、确认页不显示 Key、自定义及手动模型 ID；后端验证五种服务商首次配置时从 mock 切换、保留记录并持久化。
4. 图标：将终端三行小猫转换为同一套 SVG、PNG 和多尺寸 ICO；用于侧栏、欢迎页、窗口和两个 EXE。生成源为 `scripts/generate_app_icon.py`，已提交生成资源，打包无需 Pillow。
5. 成品验收：真实解压 ZIP、中文和空格路径、无系统 Python/Node；实际 UI 配置 DeepSeek（连接本地测试接口）、保存、重启，使用保存的 Key 完成流式请求；验证 Key 不进入摘要、localStorage 或日志，两个 EXE 均嵌入全部尺寸图标，退出清理后端。

注意：验收使用本地 HTTP 测试接口和无效测试 Key；没有调用或消耗真实供应商账号。官方账号权限及外部 API 可用性仍取决于用户填写的 Key。

## 参考

## v2.0.3：统一客户端控件

- 以 `Select`、`Button` 共用组件替换模型设置、服务商配置与输入时机中的原生下拉及零散按钮；同步整理弹窗层级、操作区、权限弹窗和复选框样式。视觉规则见 `desktop-ui-style.md`。
- 客户端 57 项测试通过，包括禁用项跳过、方向键 / Home / End / 输入定位、Enter、Escape 与 Tab，及配置确认前不保存和切换服务商清空 Key。
- 实际浏览器连接隔离后端，完成服务商配置、模型选择，并检查常规与 820×620 窗口的菜单边界、白色菜单背景、Escape 保留外层弹窗与 Tab 关闭菜单；生成截图后进行视觉检查。
- 实际 ZIP 验收通过：没有原生 select，菜单在窗口内可见，四个服务商可配置，Key 保存及重启后的认证流式请求成功，退出清理后端，图标资源完整；测试未使用真实 API Key。

## 参考资料

- [PyInstaller 运行时路径与 sys.executable](https://pyinstaller.org/en/stable/runtime-information.html)
- [PyInstaller 冻结应用的外部程序调用注意事项](https://pyinstaller.org/en/stable/common-issues-and-pitfalls.html)
- [electron-builder 资源打包](https://www.electron.build/v26/docs/contents/)；本项目使用的 24.x 字段以安装包自带类型定义为准。
