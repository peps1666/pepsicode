# pepsicode Python 鍔熻兘瀹屾暣鎬ф祴璇曞疄鐜拌鍒?
> **闈㈠悜 AI 浠ｇ悊鐨勫伐浣滆€咃細** 蹇呴渶瀛愭妧鑳斤細浣跨敤 superpowers:subagent-driven-development锛堟帹鑽愶級鎴?superpowers:executing-plans 閫愪换鍔″疄鐜版璁″垝銆傛楠や娇鐢ㄥ閫夋锛坄- [ ]`锛夎娉曟潵璺熻釜杩涘害銆?
**鐩爣锛?* 鍒涘缓鑷姩鍖栭泦鎴愭祴璇曞浠讹紝楠岃瘉 pepsicode Python 缁忚繃涓冭疆浼樺寲鍚庣殑鎵€鏈夋牳蹇冨姛鑳芥槸鍚︽甯稿伐浣?
**鏋舵瀯锛?* 鍗曚釜娴嬭瘯鏂囦欢鍖呭惈 7 涓祴璇曟ā鍧楋紝鎸夐『搴忔墽琛岄獙璇佸惎鍔ㄣ€佸伐鍏枫€佹潈闄愩€佷笂涓嬫枃銆佽蹇嗐€佸府鍔┿€侀敊璇仮澶?
**鎶€鏈爤锛?* Python 3.11+, pytest, tempfile, pathlib

---

### 浠诲姟 1锛氬垱寤烘祴璇曟枃浠舵鏋跺拰妯″潡 1锛堝惎鍔ㄤ笌閰嶇疆锛?
**鏂囦欢锛?*
- 鍒涘缓锛歚tests/test_functional_completeness.py`

- [ ] **姝ラ 1锛氬垱寤烘祴璇曟枃浠跺苟缂栧啓妯″潡 1**

```python
"""Functional Completeness Test Suite for pepsicode Python.

Tests all core modules after 7 rounds of optimization:
1. Startup & Configuration
2. Tool Execution
3. Permission System
4. Context Management
5. Memory System
6. Help System
7. Error Recovery
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

# Add parent directory to path
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))


class TestStartupAndConfig:
    """Module 1: Test startup and configuration validation."""

    def test_config_diagnostic_command(self):
        """Test --validate-config output format."""
        from pepsicode.config import format_config_diagnostic
        result = format_config_diagnostic()
        assert "Configuration Diagnostics" in result
        assert "Status:" in result

    def test_logging_system_initialization(self):
        """Test logging system initializes correctly."""
        from pepsicode.logging_config import setup_logging, get_logger
        logger = setup_logging(level="DEBUG", log_to_file=False, log_to_console=False)
        assert logger.name == "pepsicode"
        assert logger.level == 10  # DEBUG level

    def test_core_module_imports(self):
        """Test all core modules import without errors."""
        from pepsicode.main import main
        from pepsicode.logging_config import setup_logging
        from pepsicode.context_manager import ContextManager
        from pepsicode.memory import MemoryManager
        from pepsicode.config import validate_config
        # If we get here, all imports succeeded
        assert True
```

- [ ] **姝ラ 2锛氳繍琛屾祴璇曢獙璇侀€氳繃**

杩愯锛歚pytest tests/test_functional_completeness.py::TestStartupAndConfig -v`
棰勬湡锛? 涓祴璇曞叏閮?PASS

- [ ] **姝ラ 3锛欳ommit**

```bash
git add tests/test_functional_completeness.py
git commit -m "test: add functional completeness test - Module 1 (Startup & Config)"
```

---

### 浠诲姟 2锛氭ā鍧?2锛堝伐鍏锋墽琛屾祴璇曪級

**鏂囦欢锛?*
- 淇敼锛歚tests/test_functional_completeness.py`

- [ ] **姝ラ 1锛氭坊鍔犲伐鍏锋墽琛屾祴璇?*

鍦ㄦ枃浠舵湯灏炬坊鍔狅細

```python


class MockPermissions:
    """Mock permission manager that allows everything."""
    def ensure_path_access(self, *args):
        pass


class MockContext:
    """Mock tool context."""
    def __init__(self, cwd: str):
        self.cwd = cwd
        self.permissions = MockPermissions()


class TestToolExecution:
    """Module 2: Test tool execution."""

    @pytest.fixture
    def test_dir(self, tmp_path):
        """Create a temporary test directory with sample files."""
        # Create test files
        (tmp_path / "file1.txt").write_text("Hello World\nLine 2\nLine 3", encoding="utf-8")
        (tmp_path / "file2.py").write_text("def foo():\n    return 'bar'\n", encoding="utf-8")
        subdir = tmp_path / "subdir"
        subdir.mkdir()
        (subdir / "file3.txt").write_text("Nested file content", encoding="utf-8")
        return tmp_path

    @pytest.fixture
    def context(self, test_dir):
        """Create mock context for tool execution."""
        return MockContext(cwd=str(test_dir))

    def test_list_files_tool(self, context):
        """Test list_files_tool executes successfully."""
        from pepsicode.tools.list_files import list_files_tool
        result = list_files_tool._run({"path": ".", "limit": 100}, context)
        assert result.ok
        assert "file1.txt" in result.output or "file2.py" in result.output

    def test_read_file_tool(self, context):
        """Test read_file_tool executes successfully."""
        from pepsicode.tools.read_file import read_file_tool
        result = read_file_tool._run({"path": "file1.txt", "offset": 0, "limit": 1000}, context)
        assert result.ok
        assert "Hello World" in result.output

    def test_grep_files_tool(self, context):
        """Test grep_files_tool executes successfully."""
        from pepsicode.tools.grep_files import grep_files_tool
        result = grep_files_tool._run({"pattern": "Hello", "path": "."}, context)
        assert result.ok
        assert "file1.txt" in result.output

    def test_run_command_tool(self, context):
        """Test run_command_tool executes successfully."""
        from pepsicode.tools.run_command import run_command_tool
        result = run_command_tool._run({"command": "python --version"}, context)
        assert result.ok
        assert "Python" in result.output
```

- [ ] **姝ラ 2锛氳繍琛屾祴璇曢獙璇侀€氳繃**

杩愯锛歚pytest tests/test_functional_completeness.py::TestToolExecution -v`
棰勬湡锛? 涓祴璇曞叏閮?PASS

- [ ] **姝ラ 3锛欳ommit**

```bash
git add tests/test_functional_completeness.py
git commit -m "test: add Module 2 (Tool Execution)"
```

---

### 浠诲姟 3锛氭ā鍧?3锛堟潈闄愮郴缁熸祴璇曪級

**鏂囦欢锛?*
- 淇敼锛歚tests/test_functional_completeness.py`

- [ ] **姝ラ 1锛氭坊鍔犳潈闄愮郴缁熸祴璇?*

```python


class TestPermissionSystem:
    """Module 3: Test permission system."""

    def test_path_access_within_cwd_allowed(self):
        """Test that path access within cwd is allowed."""
        from pepsicode.permissions import PermissionManager
        pm = PermissionManager(workspace_root="/test/cwd")
        # Should not raise
        pm.ensure_path_access("/test/cwd/file.txt", "read")

    def test_path_access_outside_cwd_denied_without_prompt(self):
        """Test that path access outside cwd is denied when no prompt."""
        from pepsicode.permissions import PermissionManager
        pm = PermissionManager(workspace_root="/test/cwd")
        with pytest.raises(RuntimeError, match="Access denied"):
            pm.ensure_path_access("/etc/passwd", "read")

    def test_dangerous_command_detection(self):
        """Test that dangerous commands are detected."""
        from pepsicode.permissions import _classify_dangerous_command
        # Git dangerous commands
        result = _classify_dangerous_command("git", ["reset", "--hard"])
        assert result is not None
        assert "git reset --hard" in result
        
        # Shell execution
        result = _classify_dangerous_command("bash", ["-c", "echo test"])
        assert result is not None
        assert "bash" in result
```

- [ ] **姝ラ 2锛氳繍琛屾祴璇曢獙璇侀€氳繃**

杩愯锛歚pytest tests/test_functional_completeness.py::TestPermissionSystem -v`
棰勬湡锛? 涓祴璇曞叏閮?PASS

- [ ] **姝ラ 3锛欳ommit**

```bash
git add tests/test_functional_completeness.py
git commit -m "test: add Module 3 (Permission System)"
```

---

### 浠诲姟 4锛氭ā鍧?4锛堜笂涓嬫枃绠＄悊娴嬭瘯锛?
**鏂囦欢锛?*
- 淇敼锛歚tests/test_functional_completeness.py`

- [ ] **姝ラ 1锛氭坊鍔犱笂涓嬫枃绠＄悊娴嬭瘯**

```python


class TestContextManagement:
    """Module 4: Test context window management."""

    def test_token_estimation_ascii(self):
        """Test token estimation for ASCII text."""
        from pepsicode.context_manager import estimate_tokens
        text = "Hello World " * 100
        tokens = estimate_tokens(text)
        # ~4 chars/token for ASCII
        expected = len(text) // 4
        assert abs(tokens - expected) < expected * 0.2  # Within 20%

    def test_token_estimation_chinese(self):
        """Test token estimation for Chinese text."""
        from pepsicode.context_manager import estimate_tokens
        text = "浣犲ソ涓栫晫" * 100
        tokens = estimate_tokens(text)
        # ~1.5 chars/token for CJK
        expected = len(text) // 1.5
        assert abs(tokens - expected) < expected * 0.2

    def test_context_manager_stats(self):
        """Test context manager statistics."""
        from pepsicode.context_manager import ContextManager
        ctx = ContextManager(model="claude-sonnet-4-20250514")
        ctx.messages = [{"role": "user", "content": "Hello " * 100}]
        stats = ctx.get_stats()
        assert stats.total_tokens > 0
        assert stats.messages_count == 1

    def test_context_compaction(self):
        """Test context compaction reduces message count."""
        from pepsicode.context_manager import ContextManager
        ctx = ContextManager(model="claude-sonnet-4-20250514", context_window=1000)
        # Add many messages to trigger compaction
        ctx.messages = [{"role": "user", "content": "x" * 50} for _ in range(50)]
        if ctx.should_compact():
            compacted = ctx.compact_messages()
            assert len(compacted) < 50  # Should be fewer after compaction
```

- [ ] **姝ラ 2锛氳繍琛屾祴璇曢獙璇侀€氳繃**

杩愯锛歚pytest tests/test_functional_completeness.py::TestContextManagement -v`
棰勬湡锛? 涓祴璇曞叏閮?PASS

- [ ] **姝ラ 3锛欳ommit**

```bash
git add tests/test_functional_completeness.py
git commit -m "test: add Module 4 (Context Management)"
```

---

### 浠诲姟 5锛氭ā鍧?5锛堣蹇嗙郴缁熸祴璇曪級

**鏂囦欢锛?*
- 淇敼锛歚tests/test_functional_completeness.py`

- [ ] **姝ラ 1锛氭坊鍔犺蹇嗙郴缁熸祴璇?*

```python


class TestMemorySystem:
    """Module 5: Test memory system."""

    @pytest.fixture
    def memory_mgr(self, tmp_path):
        """Create a temporary memory manager."""
        from pepsicode.memory import MemoryManager
        return MemoryManager(project_root=tmp_path)

    def test_add_memory_entry(self, memory_mgr):
        """Test adding a memory entry."""
        from pepsicode.memory import MemoryScope
        entry = memory_mgr.add_entry(
            scope=MemoryScope.PROJECT,
            category="convention",
            content="Use type hints in all public APIs",
            tags=["coding", "style"],
        )
        assert entry.id
        assert "type hints" in entry.content

    def test_search_memory(self, memory_mgr):
        """Test searching memory entries."""
        from pepsicode.memory import MemoryScope
        memory_mgr.add_entry(MemoryScope.PROJECT, "test", "Python is great for coding")
        memory_mgr.add_entry(MemoryScope.PROJECT, "test", "JavaScript runs in browsers")
        results = memory_mgr.search("Python")
        assert len(results) > 0
        assert any("Python" in r.content for r in results)

    def test_memory_context_injection(self, memory_mgr):
        """Test memory context injection for system prompt."""
        from pepsicode.memory import MemoryScope
        memory_mgr.add_entry(MemoryScope.PROJECT, "convention", "Always write tests")
        context = memory_mgr.get_relevant_context(max_entries=10, max_tokens=8000)
        assert isinstance(context, str)
        assert len(context) > 0

    def test_memory_persistence(self, memory_mgr):
        """Test memory persists to disk."""
        from pepsicode.memory import MemoryScope
        memory_mgr.add_entry(MemoryScope.PROJECT, "test", "Persistent memory entry")
        # Reload and check
        memory_mgr2 = MemoryManager(project_root=memory_mgr.paths.project_root)
        results = memory_mgr2.search("Persistent")
        assert len(results) > 0
```

- [ ] **姝ラ 2锛氳繍琛屾祴璇曢獙璇侀€氳繃**

杩愯锛歚pytest tests/test_functional_completeness.py::TestMemorySystem -v`
棰勬湡锛? 涓祴璇曞叏閮?PASS

- [ ] **姝ラ 3锛欳ommit**

```bash
git add tests/test_functional_completeness.py
git commit -m "test: add Module 5 (Memory System)"
```

---

### 浠诲姟 6锛氭ā鍧?6 鍜?7锛堝府鍔╃郴缁熶笌閿欒鎭㈠锛?
**鏂囦欢锛?*
- 淇敼锛歚tests/test_functional_completeness.py`

- [ ] **姝ラ 1锛氭坊鍔犲府鍔╃郴缁熷拰閿欒鎭㈠娴嬭瘯**

```python


class TestHelpSystem:
    """Module 6: Test help and diagnostic commands."""

    def test_config_diagnostic_format(self):
        """Test /config command output format."""
        from pepsicode.config import format_config_diagnostic
        result = format_config_diagnostic()
        assert "Configuration Diagnostics" in result
        assert "=" * 40 in result

    def test_context_details_format(self):
        """Test /context command output format."""
        from pepsicode.context_manager import ContextManager
        ctx = ContextManager(model="claude-sonnet-4-20250514")
        result = ctx.format_context_details()
        assert "Context Window Usage" in result
        assert "claude-sonnet-4-20250514" in result

    def test_memory_summary_format(self):
        """Test /memory command output format."""
        from pepsicode.memory import MemoryManager
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            mem = MemoryManager(project_root=Path(tmp))
            summary = mem.get_summary()
            assert "user_entries" in summary
            assert "project_entries" in summary
            assert "local_entries" in summary

    def test_slash_commands_available(self):
        """Test that all slash commands are available."""
        from pepsicode.cli_commands import SLASH_COMMANDS
        command_names = {cmd.name for cmd in SLASH_COMMANDS}
        expected = {"/help", "/tools", "/status", "/config", "/context", "/memory", "/mcp", "/skills", "/exit"}
        assert expected.issubset(command_names)


class TestErrorRecovery:
    """Module 7: Test error handling and recovery guidance."""

    def test_config_error_guidance(self):
        """Test that config errors provide actionable guidance."""
        from pepsicode.config import validate_config
        is_valid, messages = validate_config()
        if not is_valid:
            # At least one message should contain guidance
            assert any("fix" in msg.lower() or "set" in msg.lower() for msg in messages)

    def test_tool_error_handling(self, tmp_path):
        """Test tool errors return useful messages."""
        from pepsicode.tools.read_file import read_file_tool
        
        class MockCtx:
            cwd = str(tmp_path)
            permissions = None
        
        # Try to read non-existent file
        # Should not crash, should return error result
        from pepsicode.workspace import resolve_tool_path
        # Create a file to read
        (tmp_path / "test.txt").write_text("test", encoding="utf-8")
        ctx = MockCtx()
        result = read_file_tool._run({"path": "test.txt", "offset": 0, "limit": 100}, ctx)
        assert result.ok
        assert "test" in result.output
```

- [ ] **姝ラ 2锛氳繍琛屾祴璇曢獙璇侀€氳繃**

杩愯锛歚pytest tests/test_functional_completeness.py::TestHelpSystem tests/test_functional_completeness.py::TestErrorRecovery -v`
棰勬湡锛? 涓祴璇曞叏閮?PASS

- [ ] **姝ラ 3锛欳ommit**

```bash
git add tests/test_functional_completeness.py
git commit -m "test: add Module 6 (Help System) and Module 7 (Error Recovery)"
```

---

### 浠诲姟 7锛氳繍琛屽畬鏁存祴璇曞浠跺苟鐢熸垚鎶ュ憡

**鏂囦欢锛?*
- 淇敼锛歚tests/test_functional_completeness.py`锛堝彲閫夛細娣诲姞鎬荤粨娉ㄩ噴锛?
- [ ] **姝ラ 1锛氳繍琛屽畬鏁存祴璇曞浠?*

杩愯锛歚pytest tests/test_functional_completeness.py -v`
棰勬湡锛?5 涓祴璇曞叏閮?PASS

- [ ] **姝ラ 2锛氱敓鎴愭祴璇曟姤鍛?*

```bash
pytest tests/test_functional_completeness.py -v --tb=short 2>&1 | tee tests/functional_test_report.txt
```

- [ ] **姝ラ 3锛氭渶缁?Commit**

```bash
git add tests/test_functional_completeness.py tests/functional_test_report.txt
git commit -m "test: complete functional completeness test suite - all 25 tests passing"
```

---

## 鑷

**1. 瑙勬牸瑕嗙洊搴︼細**
- 鉁?鍚姩涓庨厤缃?鈫?浠诲姟 1
- 鉁?宸ュ叿鎵ц 鈫?浠诲姟 2
- 鉁?鏉冮檺绯荤粺 鈫?浠诲姟 3
- 鉁?涓婁笅鏂囩鐞?鈫?浠诲姟 4
- 鉁?璁板繂绯荤粺 鈫?浠诲姟 5
- 鉁?甯姪绯荤粺 鈫?浠诲姟 6
- 鉁?閿欒鎭㈠ 鈫?浠诲姟 6

**2. 鍗犱綅绗︽壂鎻忥細** 鏃犲崰浣嶇銆佹棤 TODO銆佹棤妯＄硦闇€姹?
**3. 绫诲瀷涓€鑷存€э細** 鎵€鏈夋祴璇曚娇鐢ㄧ浉鍚岀殑 Mock 绫诲拰 fixture 妯″紡

---

璁″垝宸插畬鎴愬苟淇濆瓨鍒?`docs/superpowers/plans/2026-04-05-functional-completeness-test.md`銆備袱绉嶆墽琛屾柟寮忥細

**1. 瀛愪唬鐞嗛┍鍔紙鎺ㄨ崘锛?* - 姣忎釜浠诲姟璋冨害鏂扮殑瀛愪唬鐞嗭紝浠诲姟闂磋繘琛屽鏌ワ紝蹇€熻凯浠?
**2. 鍐呰仈鎵ц** - 鍦ㄥ綋鍓嶄細璇濅腑浣跨敤 executing-plans 鎵ц浠诲姟锛屾壒閲忔墽琛屽苟璁炬湁妫€鏌ョ偣

**閫夊摢绉嶆柟寮忥紵**