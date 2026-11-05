# pepsicode Python — Linux / macOS 平台适配审计

> 审计日期: 2026-04-06
> 审计范围: `pepsicode/` 下所有 `.py` 文件的跨平台兼容性

## 总结

**结论：代码已具备良好的跨平台框架，绝大部分平台分支已正确实现。**

发现 **3 个真正的 Bug**、**4 个潜在问题** 和 **2 个强烈建议**。

---

## 🔴 真正的 Bug（必须修复）

### Bug 1: Unix raw mode 中 `sys.stdin.read(1)` 阻塞问题

**文件**: `tty_app.py:1366-1382`

**问题**: `tty.setraw()` 把终端设为 raw mode，但 Python 层的 `BufferedReader` 仍会尝试读取完整的 UTF-8 多字节序列。如果用户输入中文 Emoji（需要 3-4 字节的 UTF-8），第一个字节到达后 TextIOWrapper 可能尝试读取后续字节，若后续字节因时序原因稍有延迟，就可能短暂阻塞。

**修复方案**: 使用 `os.read()` 读取原始字节，然后手动 decode：
```python
fd = sys.stdin.fileno()
chunk_bytes = os.read(fd, 4096)
chunk = chunk_bytes.decode("utf-8", errors="replace")
```

### Bug 2: Unix raw mode 中 stdout 输出处理

**问题**: `tty.setraw()` 同时影响 stdout 的行处理——raw mode 会禁用 output postprocessing（`OPOST`），导致 `\n` 不再被自动翻译为 `\r\n`。

**修复方案**: 用 `tty.setcbreak()` 替代 `tty.setraw()`，或手动设置 termios 属性，保留 `OPOST`。

### Bug 3: Windows 下 `select.select()` 不可用于 stdin

**问题**: Windows 的 `select.select()` 不支持标准输入流，导致事件循环在 Windows 上行为异常。

**修复方案**: Windows 上使用 `msvcrt.kbhit()` + `msvcrt.getwch()` 替代。

---

## 🟡 潜在问题

1. **路径分隔符** — 大部分使用 `Path` 处理，但仍有少量硬编码 `/`
2. **文件权限** — `os.chmod` 在 Windows 上行为不同
3. **信号处理** — Unix `SIGWINCH` 在 Windows 上不可用
4. **进程管理** — `os.kill()` 在 Windows 上不支持 SIGTERM

---

## 强烈建议

1. **统一 IO 层** — 抽象跨平台的 stdin/stdout 处理
2. **增加 CI 测试** — 在 Linux/macOS/Windows 三平台上运行测试
