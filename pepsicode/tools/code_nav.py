from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

from pepsicode.execution.filesystem import FILES
from pepsicode.intelligence.fallback import references, source_files
from pepsicode.intelligence.fallback import symbols as syntax_symbols
from pepsicode.tooling import ToolDefinition, ToolResult
from pepsicode.workspace import resolve_tool_path

# ---------------------------------------------------------------------------
# AST Analysis Helpers
# ---------------------------------------------------------------------------


def _get_symbol_type(node: ast.AST) -> str | None:
    """Get symbol type from AST node."""
    if isinstance(node, ast.ClassDef):
        return "class"
    elif isinstance(node, ast.FunctionDef) or isinstance(node, ast.AsyncFunctionDef):
        return "function"
    elif isinstance(node, ast.Assign) or isinstance(node, ast.AnnAssign):
        return "variable"
    return None


def _extract_symbols_from_file(file_path: Path) -> list[dict[str, Any]]:
    """Extract all symbols from a Python file using AST."""
    if not file_path.exists():
        return []

    try:
        content = FILES.read_text(file_path)
        tree = ast.parse(content, filename=str(file_path))
    except (SyntaxError, UnicodeDecodeError):
        return []

    symbols = []

    for node in ast.walk(tree):
        symbol_type = _get_symbol_type(node)
        if symbol_type is None:
            continue

        name = getattr(node, "name", None)
        if not name:
            continue

        # Get line number
        lineno = getattr(node, "lineno", 0)

        # Get docstring if exists
        docstring = ast.get_docstring(node)
        docstring_preview = docstring[:100] if docstring else ""

        # Get arguments for functions
        args = []
        if symbol_type == "function" and hasattr(node, "args"):
            for arg in node.args.args:
                arg_name = arg.arg
                arg_type = ""
                if arg.annotation:
                    try:
                        arg_type = ast.unparse(arg.annotation)
                    except Exception:
                        arg_type = "?"
                args.append(f"{arg_name}: {arg_type}" if arg_type else arg_name)

        # Get decorators
        decorators = []
        for dec in getattr(node, "decorator_list", []):
            try:
                decorators.append(ast.unparse(dec))
            except Exception:
                decorators.append("?")

        # Get class bases
        bases = []
        if symbol_type == "class" and hasattr(node, "bases"):
            for base in node.bases:
                try:
                    bases.append(ast.unparse(base))
                except Exception:
                    bases.append("?")

        symbols.append(
            {
                "type": symbol_type,
                "name": name,
                "line": lineno,
                "docstring": docstring_preview,
                "args": args if symbol_type == "function" else [],
                "decorators": decorators,
                "bases": bases if symbol_type == "class" else [],
            }
        )

    return symbols


def _find_symbol_references(file_path: Path, symbol_name: str) -> list[dict[str, Any]]:
    """Find exact identifier tokens, excluding comments and string literals."""
    if not file_path.exists():
        return []

    try:
        return references(file_path, symbol_name)
    except (OSError, UnicodeDecodeError):
        return []


# ---------------------------------------------------------------------------
# Tool Implementations
# ---------------------------------------------------------------------------


def _validate_find_symbols(input_data: dict) -> dict:
    path = input_data.get("path", ".")
    symbol_type = input_data.get("symbol_type", "all")
    if symbol_type not in ("all", "class", "function", "variable"):
        raise ValueError("symbol_type must be one of: all, class, function, variable")
    return {"path": path, "symbol_type": symbol_type}


def _run_find_symbols(input_data: dict, context) -> ToolResult:
    """Find Python AST and multi-language declaration candidates."""
    search_path = resolve_tool_path(context, input_data["path"], "read")
    symbol_type = input_data["symbol_type"]

    if not search_path.exists():
        return ToolResult(ok=False, output=f"Path not found: {search_path}")

    py_files = source_files(search_path)

    all_symbols = []
    for py_file in py_files:
        resolve_tool_path(context, str(py_file), "read")
        symbols = syntax_symbols(py_file)
        for sym in symbols:
            sym["file"] = str(py_file)
            all_symbols.append(sym)

    # Filter by type
    if symbol_type != "all":
        all_symbols = [s for s in all_symbols if s["type"] == symbol_type]

    if not all_symbols:
        return ToolResult(
            ok=True,
            output=f"No symbols found in {input_data['path']}",
        )

    # Format output
    lines = [
        f"Found {len(all_symbols)} symbol(s) in {input_data['path']} (Python AST / lexical declarations; use code_intelligence for LSP):",
        "",
    ]

    by_file: dict[str, list] = {}
    for sym in all_symbols:
        by_file.setdefault(sym["file"], []).append(sym)

    for file, symbols in by_file.items():
        lines.append(f"\U0001f4c4 {file}")
        for sym in symbols:
            icon = {"class": "[C]", "function": "[F]", "variable": "[V]"}.get(sym["type"], "[?]")
            type_label = sym["type"][:3].upper()

            extra = ""
            if sym["type"] == "function" and sym["args"]:
                extra = f"({', '.join(sym['args'])})"
            elif sym["type"] == "class" and sym["bases"]:
                extra = f"({', '.join(sym['bases'])})"

            lines.append(f"  {icon} [{type_label}] {sym['name']}{extra} (line {sym['line']})")
            if sym["docstring"]:
                lines.append(f"      💬 {sym['docstring']}")
        lines.append("")

    return ToolResult(ok=True, output="\n".join(lines))


def _validate_find_references(input_data: dict) -> dict:
    symbol_name = input_data.get("symbol_name")
    if not isinstance(symbol_name, str) or not symbol_name.strip():
        raise ValueError("symbol_name is required")
    path = input_data.get("path", ".")
    return {"symbol_name": symbol_name.strip(), "path": path}


def _run_find_references(input_data: dict, context) -> ToolResult:
    """Find all references to a symbol."""
    symbol_name = input_data["symbol_name"]
    search_path = resolve_tool_path(context, input_data["path"], "read")

    if not search_path.exists():
        return ToolResult(ok=False, output=f"Path not found: {search_path}")

    py_files = source_files(search_path)

    all_refs = []
    for py_file in py_files:
        resolve_tool_path(context, str(py_file), "read")
        refs = _find_symbol_references(py_file, symbol_name)
        all_refs.extend(refs)

    if not all_refs:
        return ToolResult(
            ok=True,
            output=f"No references found for '{symbol_name}' in {input_data['path']}",
        )

    # Format output
    lines = [
        f"Found {len(all_refs)} lexical reference candidate(s) for '{symbol_name}' (not scope-resolved; use code_intelligence for LSP):",
        "",
    ]

    by_file: dict[str, list] = {}
    for ref in all_refs:
        rel_path = Path(ref["file"])
        by_file.setdefault(str(rel_path), []).append(ref)

    for file, refs in by_file.items():
        lines.append(f"\U0001f4c4 {file} ({len(refs)} refs)")
        for ref in refs[:20]:  # Limit to 20 per file
            lines.append(f"  L{ref['line']}: {ref['code']}")
        if len(refs) > 20:
            lines.append(f"  ... and {len(refs) - 20} more")
        lines.append("")

    return ToolResult(ok=True, output="\n".join(lines))


def _validate_get_ast_info(input_data: dict) -> dict:
    file_path = input_data.get("file_path")
    if not isinstance(file_path, str) or not file_path:
        raise ValueError("file_path is required")
    return {"file_path": file_path}


def _run_get_ast_info(input_data: dict, context) -> ToolResult:
    """Get AST information for a Python file."""
    target = resolve_tool_path(context, input_data["file_path"], "read")

    if not target.exists():
        return ToolResult(ok=False, output=f"File not found: {target}")

    try:
        content = FILES.read_text(target)
        tree = ast.parse(content, filename=str(target))
    except SyntaxError as e:
        return ToolResult(ok=False, output=f"Syntax error: {e}")
    except UnicodeDecodeError as e:
        return ToolResult(ok=False, output=f"Encoding error: {e}")

    # Count statistics
    classes = sum(1 for _ in ast.walk(tree) if isinstance(_, ast.ClassDef))
    functions = sum(1 for _ in ast.walk(tree) if isinstance(_, (ast.FunctionDef, ast.AsyncFunctionDef)))
    imports = sum(1 for _ in ast.walk(tree) if isinstance(_, (ast.Import, ast.ImportFrom)))

    # Get imports
    import_list = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                import_list.append(f"import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            for alias in node.names:
                import_list.append(f"from {module} import {alias.name}")

    # Format output
    lines = [
        f"AST Info for {input_data['file_path']}",
        "=" * 50,
        "",
        f"Lines: {len(content.splitlines())}",
        f"Classes: {classes}",
        f"Functions: {functions}",
        f"Imports: {imports}",
        "",
        "Imports:",
    ]

    for imp in import_list[:20]:
        lines.append(f"  {imp}")

    if len(import_list) > 20:
        lines.append(f"  ... and {len(import_list) - 20} more")

    return ToolResult(ok=True, output="\n".join(lines))


# ---------------------------------------------------------------------------
# Tool Definitions
# ---------------------------------------------------------------------------

find_symbols_tool = ToolDefinition(
    name="find_symbols",
    description="Find Python AST symbols and multi-language lexical declarations in source files. Use code_intelligence for language-server-backed navigation.",
    input_schema={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "File or directory path to search (default: current directory)"},
            "symbol_type": {
                "type": "string",
                "enum": ["all", "class", "function", "variable"],
                "description": "Filter by symbol type (default: all)",
            },
        },
    },
    validator=_validate_find_symbols,
    run=_run_find_symbols,
    concurrency_safe=True,
)

find_references_tool = ToolDefinition(
    name="find_references",
    description="Find exact identifier tokens across source files, excluding comments and strings. Results are lexical candidates; use code_intelligence for scope-resolved references and rename_symbol for renaming.",
    input_schema={
        "type": "object",
        "properties": {
            "symbol_name": {"type": "string", "description": "Name of the symbol to find references for"},
            "path": {"type": "string", "description": "File or directory path to search (default: current directory)"},
        },
        "required": ["symbol_name"],
    },
    validator=_validate_find_references,
    run=_run_find_references,
    concurrency_safe=True,
)

get_ast_info_tool = ToolDefinition(
    name="get_ast_info",
    description="Get AST information for a Python file including structure, imports, and statistics. Use this to understand file organization.",
    input_schema={
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": "Path to Python file"},
        },
        "required": ["file_path"],
    },
    validator=_validate_get_ast_info,
    run=_run_get_ast_info,
    concurrency_safe=True,
)
