"""AST-only, read-only code graph builder.

The parser intentionally has no import, subprocess or network path. Target
project files are read as bytes and parsed with :mod:`ast`; calls are resolved
only from static syntax and local import aliases.
"""

from __future__ import annotations

import ast
import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

GRAPH_SCHEMA = "mokoa.code-graph/v1"
_SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    "__pycache__",
    ".venv",
    "venv",
    "node_modules",
    "dist",
    "build",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
}
_BUILTINS = {
    "abs", "all", "any", "bool", "bytes", "callable", "chr", "dict", "dir",
    "enumerate", "eval", "exec", "filter", "float", "format", "frozenset",
    "getattr", "globals", "hasattr", "hash", "help", "hex", "id", "input",
    "int", "isinstance", "issubclass", "iter", "len", "list", "locals", "map",
    "max", "memoryview", "min", "next", "object", "oct", "open", "ord", "pow",
    "print", "property", "range", "repr", "reversed", "round", "set", "setattr",
    "slice", "sorted", "str", "sum", "super", "tuple", "type", "vars", "zip",
    "__import__",
}


@dataclass(frozen=True)
class Node:
    id: str
    kind: str
    name: str
    path: str | None
    line: int | None = None
    end_line: int | None = None
    digest: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "name": self.name,
            "path": self.path,
            "line": self.line,
            "end_line": self.end_line,
            "digest": self.digest,
        }


@dataclass(frozen=True)
class Edge:
    source: str
    target: str
    kind: str
    line: int | None = None
    label: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "target": self.target,
            "kind": self.kind,
            "line": self.line,
            "label": self.label,
        }


@dataclass(frozen=True)
class Diagnostic:
    code: str
    message: str
    path: str | None = None
    line: int | None = None
    symbol: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "path": self.path,
            "line": self.line,
            "symbol": self.symbol,
        }


@dataclass(frozen=True)
class CodeGraph:
    root: str
    nodes: tuple[Node, ...]
    edges: tuple[Edge, ...]
    diagnostics: tuple[Diagnostic, ...]
    files: dict[str, str]

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": GRAPH_SCHEMA,
            "root": self.root,
            "nodes": [item.as_dict() for item in self.nodes],
            "edges": [item.as_dict() for item in self.edges],
            "diagnostics": [item.as_dict() for item in self.diagnostics],
            "files": dict(sorted(self.files.items())),
        }

    def node(self, node_id: str) -> Node | None:
        return next((item for item in self.nodes if item.id == node_id), None)


@dataclass(frozen=True)
class GraphRefresh:
    changed_files: tuple[str, ...]
    added_edges: tuple[Edge, ...]
    removed_edges: tuple[Edge, ...]
    graph: CodeGraph


class _Module:
    def __init__(self, rel: str, module: str, is_package: bool, source: str, digest: str) -> None:
        self.rel = rel
        self.module = module
        self.is_package = is_package
        self.source = source
        self.digest = digest
        self.tree: ast.Module | None = None
        self.symbols: dict[str, Node] = {}
        self.by_name: dict[str, Node] = {}
        self.bindings: dict[str, str] = {}


def _relative_path(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def _module_name(rel: str) -> str:
    parts = rel.split("/")
    if parts[-1] == "__init__.py":
        parts = parts[:-1]
    elif parts[-1].lower().endswith(".py"):
        parts[-1] = parts[-1][:-3]
    return ".".join(parts) or "__root__"


def _discover(root: Path, max_files: int) -> list[Path]:
    if not root.is_dir():
        raise ValueError(f"代码根目录不存在: {root}")
    files: list[Path] = []
    for directory, subdirs, filenames in root.walk():
        subdirs[:] = [
            name for name in subdirs
            if name not in _SKIP_DIRS
            and not (directory / name).is_symlink()
            and not (directory / name).is_junction()
        ]
        for name in filenames:
            path = directory / name
            if path.suffix.lower() != ".py" or path.is_symlink() or not path.is_file():
                continue
            files.append(path)
            if len(files) > max_files:
                raise ValueError(f"Python 文件数超过上限 {max_files}")
    return sorted(files)


def _hash_file(path: Path) -> tuple[str, bytes]:
    raw = path.read_bytes()
    return hashlib.sha256(raw).hexdigest(), raw


def _collect_symbols(module: _Module, module_node: Node, edges: list[Edge]) -> None:
    tree = module.tree
    if tree is None:
        return

    def walk(body: list[ast.stmt], prefix: str) -> None:
        for item in body:
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                qualname = f"{prefix}{item.name}"
                node_id = f"symbol:{module.rel}:{qualname}"
                if qualname in module.symbols:
                    continue
                kind = "class" if isinstance(item, ast.ClassDef) else "function"
                node = Node(
                    id=node_id,
                    kind=kind,
                    name=qualname,
                    path=module.rel,
                    line=item.lineno,
                    end_line=getattr(item, "end_lineno", None),
                )
                module.symbols[qualname] = node
                if not prefix:
                    module.by_name.setdefault(item.name, node)
                edges.append(Edge(module_node.id, node.id, "contains", item.lineno, item.name))
                walk(item.body, f"{qualname}.")

    walk(tree.body, "")


def _resolve_module(module: str, module_to_rel: dict[str, str]) -> str | None:
    return module_to_rel.get(module)


def _relative_base(module: _Module, level: int, name: str | None) -> str | None:
    parts = module.module.split(".") if module.is_package else module.module.split(".")[:-1]
    if module.module == "__root__":
        parts = []
    if level < 1:
        return None
    if level > len(parts) + 1:
        return None
    keep = len(parts) - (level - 1)
    base = parts[:keep]
    if name:
        base.extend(name.split("."))
    return ".".join(part for part in base if part) or None


def _external_node(name: str, nodes: dict[str, Node]) -> Node:
    node_id = f"external:{name}"
    if node_id not in nodes:
        nodes[node_id] = Node(node_id, "external", name, None)
    return nodes[node_id]


def _module_target(
    name: str,
    module_to_rel: dict[str, str],
    nodes: dict[str, Node],
    edges: list[Edge],
    *,
    source: str,
    line: int,
    label: str,
) -> str:
    rel = module_to_rel.get(name)
    if rel is None:
        target = _external_node(name, nodes)
    else:
        target = nodes[f"module:{rel}"]
    edges.append(Edge(source, target.id, "imports", line, label))
    return target.id


def _parse_imports(
    module: _Module,
    module_node: Node,
    module_to_rel: dict[str, str],
    modules: dict[str, _Module],
    nodes: dict[str, Node],
    edges: list[Edge],
    diagnostics: list[Diagnostic],
) -> None:
    tree = module.tree
    if tree is None:
        return
    for item in ast.walk(tree):
        if isinstance(item, ast.Import):
            for alias in item.names:
                target_id = _module_target(
                    alias.name, module_to_rel, nodes, edges,
                    source=module_node.id, line=item.lineno, label=alias.name,
                )
                module.bindings[alias.asname or alias.name.split(".")[0]] = target_id
        elif isinstance(item, ast.ImportFrom):
            if item.level == 0:
                base = item.module or ""
            else:
                base = _relative_base(module, item.level, item.module)
                if base is None:
                    diagnostics.append(Diagnostic(
                        "invalid_relative_import",
                        f"无法解析相对导入 level={item.level} module={item.module!r}",
                        module.rel,
                        item.lineno,
                    ))
                    continue
            if not base:
                continue
            base_rel = module_to_rel.get(base)
            base_id = nodes[f"module:{base_rel}"].id if base_rel else _external_node(base, nodes).id
            edges.append(Edge(module_node.id, base_id, "imports", item.lineno, base))
            for alias in item.names:
                if alias.name == "*":
                    if base_rel is not None:
                        edges.append(Edge(module_node.id, base_id, "imports", item.lineno, f"{base}.*"))
                    break
                candidate = f"{base}.{alias.name}"
                candidate_rel = module_to_rel.get(candidate)
                target_id: str
                if candidate_rel is not None:
                    target_id = nodes[f"module:{candidate_rel}"].id
                    edges.append(Edge(module_node.id, target_id, "imports", item.lineno, candidate))
                elif base_rel is not None:
                    target_module = modules[base_rel]
                    symbol = target_module.symbols.get(alias.name)
                    if symbol is not None:
                        target_id = symbol.id
                        edges.append(Edge(module_node.id, target_id, "imports", item.lineno, candidate))
                    else:
                        target_id = base_id
                        edges.append(Edge(module_node.id, base_id, "imports", item.lineno, candidate))
                else:
                    target_id = base_id
                    edges.append(Edge(module_node.id, base_id, "imports", item.lineno, candidate))
                module.bindings[alias.asname or alias.name] = target_id


def _resolve_name(
    name: str,
    module: _Module,
) -> str | None:
    if name in module.bindings:
        return module.bindings[name]
    symbol = module.by_name.get(name)
    if symbol is not None:
        return symbol.id
    return None


def _resolve_call(
    func: ast.expr,
    module: _Module,
    modules: dict[str, _Module],
    nodes: dict[str, Node],
) -> tuple[str | None, str]:
    if isinstance(func, ast.Name):
        if func.id in _BUILTINS:
            return None, ""
        target = _resolve_name(func.id, module)
        if target is not None:
            return target, func.id
        return None, func.id
    if isinstance(func, ast.Attribute):
        if isinstance(func.value, ast.Name):
            base = _resolve_name(func.value.id, module)
            if base is not None:
                target_node = nodes.get(base)
                if target_node is not None and target_node.kind == "module" and target_node.path:
                    target_module = modules[target_node.path]
                    symbol = target_module.by_name.get(func.attr)
                    if symbol is not None:
                        return symbol.id, f"{func.value.id}.{func.attr}"
                    return base, f"{func.value.id}.{func.attr}"
                if target_node is not None and target_node.kind == "class" and target_node.path:
                    owner = modules[target_node.path]
                    method = owner.symbols.get(f"{target_node.name}.{func.attr}")
                    label = f"{func.value.id}.{func.attr}"
                    return (method.id if method else None), label
                return base, f"{func.value.id}.{func.attr}"
        return None, ast.unparse(func)
    return None, ast.unparse(func)


def _collect_calls(
    module: _Module,
    caller: str,
    node: ast.AST,
    modules: dict[str, _Module],
    nodes: dict[str, Node],
    edges: list[Edge],
    diagnostics: list[Diagnostic],
) -> None:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        qual = node.name if caller == f"module:{module.rel}" else None
        if qual is None:
            symbol = next(
                (item for item in module.symbols.values()
                 if item.line == node.lineno and item.path == module.rel),
                None,
            )
            child = symbol.id if symbol else caller
        else:
            symbol = module.symbols.get(qual)
            child = symbol.id if symbol and symbol.line == node.lineno else caller
        for statement in node.body:
            _collect_calls(module, child, statement, modules, nodes, edges, diagnostics)
        return
    if isinstance(node, ast.Call):
        target, label = _resolve_call(node.func, module, modules, nodes)
        if target is None:
            if not isinstance(node.func, ast.Name) or node.func.id not in _BUILTINS:
                diagnostics.append(Diagnostic(
                    "dynamic_call",
                    f"无法静态解析调用: {label or ast.unparse(node.func)}",
                    module.rel,
                    node.lineno,
                    caller,
                ))
        else:
            edges.append(Edge(caller, target, "calls", node.lineno, label))
    for child in ast.iter_child_nodes(node):
        _collect_calls(module, caller, child, modules, nodes, edges, diagnostics)


def _build(
    root: Path,
    *,
    max_files: int,
) -> CodeGraph:
    root = root.resolve()
    paths = _discover(root, max_files)
    modules: dict[str, _Module] = {}
    module_to_rel: dict[str, str] = {}
    files: dict[str, str] = {}
    nodes_map: dict[str, Node] = {}
    edges: list[Edge] = []
    diagnostics: list[Diagnostic] = []

    for path in paths:
        rel = _relative_path(root, path)
        digest, raw = _hash_file(path)
        files[rel] = digest
        source = raw.decode("utf-8", errors="replace")
        module_name = _module_name(rel)
        module = _Module(rel, module_name, rel.endswith("__init__.py"), source, digest)
        try:
            module.tree = ast.parse(source, filename=rel)
        except SyntaxError as exc:
            diagnostics.append(Diagnostic(
                "syntax_error",
                f"{exc.msg}",
                rel,
                exc.lineno,
            ))
        modules[rel] = module
        if module_name in module_to_rel:
            diagnostics.append(Diagnostic(
                "module_name_collision",
                f"模块名 {module_name} 同时映射到 {module_to_rel[module_name]} 和 {rel}",
                rel,
            ))
        else:
            module_to_rel[module_name] = rel
        node = Node(f"module:{rel}", "module", module_name, rel, 1, None, digest)
        nodes_map[node.id] = node

    for module in modules.values():
        node = nodes_map[f"module:{module.rel}"]
        _collect_symbols(module, node, edges)
        for symbol in module.symbols.values():
            nodes_map[symbol.id] = symbol

    for module in modules.values():
        node = nodes_map[f"module:{module.rel}"]
        if module.tree is not None:
            _parse_imports(module, node, module_to_rel, modules, nodes_map, edges, diagnostics)
            for statement in module.tree.body:
                _collect_calls(module, node.id, statement, modules, nodes_map, edges, diagnostics)

    # Drop duplicate edges (the same import can be observed in several passes)
    # while preserving deterministic ordering.
    unique_edges = sorted(set(edges), key=lambda item: (item.source, item.kind, item.target, item.line or 0, item.label))
    return CodeGraph(
        root=str(root),
        nodes=tuple(sorted(nodes_map.values(), key=lambda item: item.id)),
        edges=tuple(unique_edges),
        diagnostics=tuple(sorted(set(diagnostics), key=lambda item: (item.path or "", item.line or 0, item.code, item.message))),
        files=files,
    )


def build_graph(root: str | Path, *, max_files: int = 5000) -> CodeGraph:
    """Build a graph without importing, executing or writing target files."""

    return _build(Path(root), max_files=max_files)


class CodeGraphSession:
    """Keeps file digests and invalidates the graph when any file changes."""

    def __init__(self, root: str | Path, *, max_files: int = 5000) -> None:
        self.root = Path(root).resolve()
        self.max_files = max_files
        self.graph: CodeGraph | None = None

    def build(self) -> CodeGraph:
        self.graph = build_graph(self.root, max_files=self.max_files)
        return self.graph

    def refresh(self) -> GraphRefresh:
        if self.graph is None:
            graph = self.build()
            return GraphRefresh(tuple(sorted(graph.files)), (), (), graph)
        old = self.graph
        current_files = {
            _relative_path(self.root, path): _hash_file(path)[0]
            for path in _discover(self.root, self.max_files)
        }
        changed = tuple(sorted(
            name for name in set(old.files) | set(current_files)
            if old.files.get(name) != current_files.get(name)
        ))
        if not changed:
            return GraphRefresh((), (), (), old)
        graph = self.build()
        old_edges = set(old.edges)
        new_edges = set(graph.edges)
        return GraphRefresh(
            changed,
            tuple(sorted(new_edges - old_edges, key=lambda item: (item.source, item.kind, item.target, item.line or 0, item.label))),
            tuple(sorted(old_edges - new_edges, key=lambda item: (item.source, item.kind, item.target, item.line or 0, item.label))),
            graph,
        )
