Pepsicode - Windows x64 ZIP 便携版

使用方法
1. 将 ZIP 中的全部文件解压到一个文件夹。
2. 双击 Pepsicode.exe。无需另行安装 Python、Node。
3. 点击输入框下方「配置模型」→ 选择 DeepSeek / Kimi / GLM / 千问，填写自己的 API Key。
   首次未配置的服务商也可直接选择；点击「配置服务商 / API Key」可重新配置。
   选择模型后点击「保存并使用」。如服务商没有模型列表，可在高级设置中选择手动填写模型 ID。
4. 点击侧栏「项目」旁的 + 选择项目目录，然后新建对话。

请保持 Pepsicode.exe、PepsicodeDesktop.exe、resources、DLL 等文件的相对位置。
不要仅复制 EXE，也不要在 ZIP 预览窗口内直接运行。

数据与更新
- 配置、Key、历史会话使用当前 Windows 用户的 ~/.pepsi-code 目录。
- 项目列表保存在应用的用户数据目录；日志为该目录下的 backend.log。
- 免安装不表示数据全部存放在 ZIP 目录中。更新前关闭应用，再完整解压新版本。
- 程序启动不依赖系统 Python / Node；运行项目自己的测试、Git、Docker、语言服务器时，仍需该项目对应的工具链。

此构建未进行代码签名。仅从项目可信的 Release 下载，并可核对随附 SHA-256。
项目主页：https://github.com/peps1666/pepsicode
