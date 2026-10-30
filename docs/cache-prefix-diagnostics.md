# 缓存前缀诊断

对一个执行日志中的相邻 `model/prepared` 请求进行比较：

```powershell
python -m pepsicode.execution.prefix_diff path/to/execution.jsonl
python -m pepsicode.execution.prefix_diff path/to/journal-directory --include-cross-task
```

可传多个文件或目录；目录读取直接包含的 JSONL 文件，按记录时间合并。默认跳过不同 task_id 的相邻请求；因为每轮通常有不同 task_id，查看跨轮比较需要 `--include-cross-task`。`turn_boundary` 表示日志文件变化，`cross_task` 表示 task_id 变化；子任务与父任务也会标为跨任务，不能假设跨轮请求属于同一会话。失败或 HTTP 错误的请求不参与配对；没有结束记录的请求标为 unknown。

每对请求分别报告 `system`、`tools` 和 `messages` 的第一个结构差异路径。字符串差异的 `@char` 是零起点 Unicode 字符偏移；追加的列表项以新增项下标表示。`appended` 表示旧消息列表是新列表的完整前缀；`extended_tail` 表示末条消息的内容块被追加，之前内容保持不变。`prefix_intact` 是结构判断，包含模型和端点一致性；`invalidates_from` 中 tools/system 的次序只是诊断约定，并非已知的服务端序列化顺序。另报告下一请求已记录的输入总数、缓存读写及读取比例。原始内容不输出，未知 JSON 键使用摘要标识。

这比较的是客户端记录的请求体，并非服务商内部的 token 排列。差异位置不能直接推算缓存 token 数或证明失效原因；模型、端点、压缩、不同执行分支和服务端缓存时效都可能影响结果。日志可能包含敏感内容，应仅在可信环境分析，分享时只提供诊断输出。

工具发现触发的 `model/prefix-break` 会在对应请求对的 `prefix_breaks` 中列出。当前请求日志没有可信的实际费用，因此 `cost_usd` 为 null；不要把通用模型兜底价格当作 DeepSeek 的费用。

`tests/test_prefix_diff.py` 包含真实工具循环日志、任务状态更新、保存恢复、计划切换和压缩检查。压缩是有意更换历史的边界，会使结构前缀变化。
