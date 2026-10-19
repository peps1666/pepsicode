"""Explicitly labelled syntax/lexical fallback when no LSP server is installed."""

from __future__ import annotations

import ast
import io
import re
import tokenize
from pathlib import Path

from pepsicode.intelligence.lsp import LANGUAGES

SKIP_DIRS = {".git", "__pycache__", "node_modules", "dist", "build", ".venv", "venv", "env", ".tox", ".pepsi-code"}
_JS_TOKEN = re.compile(
    r"//[^\n]*|/\*[\s\S]*?\*/|'(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\"|`(?:\\.|[^`\\])*`|[A-Za-z_$][\w$]*"
)


def source_files(root: Path):
    import os

    if root.is_file():
        yield root
        return
    count = 0
    for parent, directories, files in os.walk(root):
        directories[:] = sorted(
            directory
            for directory in directories
            if directory not in SKIP_DIRS and not (Path(parent) / directory).is_symlink()
        )
        for name in sorted(files):
            path = Path(parent) / name
            if path.suffix in LANGUAGES and not path.is_symlink():
                yield path
                count += 1
                if count >= 2000:
                    return


def references(path: Path, name: str) -> list[dict]:
    text = path.read_text(encoding="utf-8")
    found = []
    if path.suffix in {".py", ".pyi"}:
        try:
            for token in tokenize.generate_tokens(io.StringIO(text).readline):
                if token.type == tokenize.NAME and token.string == name:
                    found.append({"line": token.start[0], "column": token.start[1] + 1})
        except (tokenize.TokenError, IndentationError):
            pass
    else:
        for token in _JS_TOKEN.finditer(text):
            if token.group() == name:
                start = token.start()
                found.append({"line": text.count("\n", 0, start) + 1, "column": start - text.rfind("\n", 0, start)})
    lines = text.splitlines()
    return [
        {
            "file": str(path),
            **item,
            "code": lines[item["line"] - 1].strip()[:160],
            "backend": "lexical (not resolved references)",
        }
        for item in found
    ]


def symbols(path: Path) -> list[dict]:
    text = path.read_text(encoding="utf-8")
    result = []
    if path.suffix in {".py", ".pyi"}:
        try:
            tree = ast.parse(text)
        except SyntaxError:
            return []
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                result.append(
                    {
                        "name": node.name,
                        "type": "class" if isinstance(node, ast.ClassDef) else "function",
                        "line": node.lineno,
                        "column": node.col_offset + 1,
                    }
                )
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    if isinstance(target, ast.Name):
                        result.append(
                            {
                                "name": target.id,
                                "type": "variable",
                                "line": target.lineno,
                                "column": target.col_offset + 1,
                            }
                        )
    else:
        # Mask comments/strings before looking for declarations, preserving offsets.
        masked = list(text)
        for token in _JS_TOKEN.finditer(text):
            if token.group().startswith(("//", "/*", "'", '"', "`")):
                for index in range(token.start(), token.end()):
                    if masked[index] != "\n":
                        masked[index] = " "
        source = "".join(masked)
        for match in re.finditer(r"\b(class|interface|enum|type|function|const|let|var)\s+([A-Za-z_$][\w$]*)", source):
            kind, name = match.groups()
            index = match.start(2)
            result.append(
                {
                    "name": name,
                    "type": "function"
                    if kind == "function"
                    else "class"
                    if kind in {"class", "interface", "enum"}
                    else "variable",
                    "line": text.count("\n", 0, index) + 1,
                    "column": index - text.rfind("\n", 0, index),
                }
            )
    return [
        {
            **item,
            "args": [],
            "bases": [],
            "decorators": [],
            "docstring": "",
            "backend": "python-ast" if path.suffix in {".py", ".pyi"} else "lexical declarations",
        }
        for item in result
    ]
