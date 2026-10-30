import os
import subprocess
import sys

import pytest

from pepsicode.tui.chrome import string_display_width, strip_ansi, wrap_text
from pepsicode.tui.markdown import render_markdownish


def test_table_renders_inline_content_without_literal_delimiter_row():
    source = "| 文件 | 状态 |\n| :--- | ---: |\n| `src/app.py` | **完成** |\n| 空 | |"
    result = strip_ansi(render_markdownish(source)).splitlines()
    assert result == ["文件 │ 状态", "src/app.py │ 完成", "空 │ "]
    assert all("`" not in line and "---" not in line for line in result)


def test_table_supports_unbordered_rows_and_literal_pipes():
    source = "名字 | 内容\n--- | ---\n`a|b` | x\\|y\n空 | "
    assert strip_ansi(render_markdownish(source)).splitlines() == ["名字 │ 内容", "a|b │ x|y", "空 │ "]


def test_non_table_pipe_text_and_code_fence_are_preserved():
    source = "| ordinary line |\n\n```sh\nprintf 'a|b'\n```"
    output = strip_ansi(render_markdownish(source))
    assert "| ordinary line |" in output
    assert "printf 'a|b'" in output


def test_inline_code_in_headings_quotes_and_long_wrapped_table():
    source = "# Use `config.yaml`\n> Read `src/main.py`\n\n| 文件 | 描述 |\n| --- | --- |\n| `目录/long-file.py` | 中文内容重复中文内容重复 |"
    rendered = strip_ansi(render_markdownish(source))
    assert "Use config.yaml" in rendered and "Read src/main.py" in rendered
    wrapped = wrap_text(render_markdownish(source), 14).splitlines()
    assert all(string_display_width(line) <= 14 for line in wrapped)
    joined = "".join(strip_ansi(line).strip() for line in wrapped)
    assert "目录/long-file.py" in joined
    assert "中文内容重复中文内容重复" in joined


def test_table_aligns_in_wide_view_and_stacks_in_narrow_view():
    source = "| 文件 | 状态 |\n| --- | --- |\n| `src/app.py` | 完成 |\n| 目录/a.py | |"
    wide = strip_ansi(render_markdownish(source, width=80)).splitlines()
    assert len({string_display_width(line.split("│", 1)[0]) for line in wide}) == 1
    narrow = strip_ansi(render_markdownish(source, width=14)).splitlines()
    assert narrow == ["文件: src/app.py", "状态: 完成", "", "文件: 目录/a.py", "状态: "]
    wrapped = wrap_text(render_markdownish(source, width=14), 14).splitlines()
    assert all(string_display_width(line) <= 14 for line in wrapped)
    assert "目录/a.py" in "".join(strip_ansi(line).strip() for line in wrapped)


def test_stream_transcript_uses_available_width_for_table():
    from pepsicode.tui.app import _build_stream_transcript_lines
    from pepsicode.tui.types import TranscriptEntry

    source = "| 路径 | 内容 |\n| --- | --- |\n| `目录/main.tex` | 论文主稿和参考资料 |"
    entry = TranscriptEntry(id=1, kind="assistant", body=source)
    narrow = _build_stream_transcript_lines([entry], 24)
    plain = "\n".join(strip_ansi(line) for line in narrow)
    assert "路径:" in plain and "内容:" in plain
    assert "目录/main.tex" in plain and "论文主稿和参考资料" in plain
    assert "`" not in plain and "---" not in plain
    assert all(string_display_width(line) <= 24 for line in narrow)


@pytest.mark.parametrize("no_color", [False, True])
def test_inline_code_colors_and_no_color(no_color):
    environment = os.environ.copy()
    environment.update(CLICOLOR_FORCE="1", TERM="xterm", PYTHONIOENCODING="utf-8")
    if no_color:
        environment["NO_COLOR"] = "1"
    else:
        environment.pop("NO_COLOR", None)
    code = (
        "from pepsicode.tui.markdown import render_markdownish; print(render_markdownish('`src/main.py` and `run()`'))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, encoding="utf-8", env=environment, check=True
    )
    assert "src/main.py" in result.stdout and "run()" in result.stdout
    assert ("\x1b[32m" in result.stdout) is not no_color
    assert ("\x1b[38;5;215m" in result.stdout) is not no_color
    assert "\x1b[48;5;236m" not in result.stdout
