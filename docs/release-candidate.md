# 2.0.7 开发候选

本页保留制定 2.0.7 候选时的历史记录；最新发行状态、构建身份及验收结果以 [GitHub Releases](https://github.com/peps1666/pepsicode/releases) 为准。本批修复以 `af533e9` 为基线，不覆盖 `v2.0.6` 等历史标签。

Python 包、运行时常量、桌面 package/lockfile 均使用 `2.0.7`。`python scripts/check_project.py` 校验它们与 README 徽章及入口本地链接。版本一致只表示候选配置一致，不证明二进制已验证。

`python -m pepsicode --version` 显示渠道、提交和 dirty 标记。桌面模型设置中的“关于 Pepsicode”以及启动日志显示本机构建身份。安装包没有可验证源码身份时显示 unknown；不会借用用户工作区的 Git 提交。

`scripts/build_desktop_backend.py` 写入 `build/build-info.json`，包含完整 Git SHA、dirty、构建时间、Python/平台及渠道，分别嵌入冻结后端并随桌面资源打包。版本不匹配会拒绝加载。冻结后端 `--self-test` 与服务端 ready 事件也返回该身份。开发或 dirty 构建不会被标为正式发布。

## 交付前仍须执行

- [ ] 选择正式发布候选提交，确认工作区干净，记录完整提交及版本。
- [ ] 在该提交执行 Python/客户端 CI 与离线 smoke，归档运行链接。
- [ ] 在 Windows x64 构建候选 ZIP，记录 SHA-256、CRC 和完整构建清单。
- [ ] 完整解压到中文和空格路径，在 PATH 不含 Python/Node 时验证消息往返与退出清理；纯净虚拟机验证单列。
- [ ] 复核入门文档和能力边界，确认成品与候选清单对应；代码变化则重建相关成品。
- [ ] 按明确发起的发布流程创建新标签、上传成品和变更说明。

手动 [Windows ZIP 工作流](../.github/workflows/windows-zip.yml) 包含后端和实际桌面生命周期 smoke；常规 CI 的组件测试不能替代这些检查。上面的清单记录候选制定时仍需完成的事项，不表示后续发行已经通过验收。
