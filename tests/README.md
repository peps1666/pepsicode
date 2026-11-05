# Python 测试

按被测职责分组：`core/`、`execution/`、`context/`、`llm/`、`cli/`、`tui/`、`server/`、`integrations/`。
`regression/` 保留跨模块回归与项目验证，`fixtures/` 存放共享测试服务。

在仓库根目录运行 `python -m pytest -q`；聚焦模块时可运行 `python -m pytest tests/execution -q`。
桌面 JavaScript 测试仍位于 `pepsicode-client/tests/`，QQ 测试仍位于 `integrations/qq/tests/`。
