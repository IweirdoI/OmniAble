"""Static code graph regressions: cycles, collisions, syntax errors, refresh."""

from __future__ import annotations

import json
from pathlib import Path

from src.code_graph import CodeGraphSession, build_graph


def _write(project: Path, name: str, source: str) -> None:
    path = project / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")


def _fixture(project: Path, marker: Path) -> None:
    _write(project, "pkg/__init__.py", "")
    _write(project, "pkg/a.py", """from .b import helper

class Process:
    def run(self):
        return helper()
""")
    _write(project, "pkg/b.py", """from .a import Process

def helper():
    return 1
""")
    _write(project, "pkg/same_a.py", "def process():\n    return 'a'\n")
    _write(project, "pkg/same_b.py", "def process():\n    return 'b'\n")
    _write(project, "dynamic.py", "def run(obj):\n    return obj.method()\n")
    _write(project, "broken.py", "def broken(:\n")
    _write(project, "external.py", "import json\n\ndef dump(value):\n    return json.dumps(value)\n")
    _write(
        project,
        "side_effect.py",
        f"open({str(marker)!r}, 'w', encoding='utf-8').write('executed')\n",
    )


def _edge_set(graph) -> set[tuple[str, str, str]]:
    return {(item.source, item.target, item.kind) for item in graph.edges}


def test_graph_contains_cycles_symbols_diagnostics_and_no_execution(tmp_path: Path) -> None:
    project = tmp_path / "project"
    marker = tmp_path / "executed.txt"
    _fixture(project, marker)

    graph = build_graph(project)
    edges = _edge_set(graph)

    assert ("module:pkg/a.py", "module:pkg/b.py", "imports") in edges
    assert ("module:pkg/b.py", "module:pkg/a.py", "imports") in edges
    assert ("symbol:pkg/a.py:Process.run", "symbol:pkg/b.py:helper", "calls") in edges
    assert "external:json" in {node.id for node in graph.nodes}
    assert ("symbol:external.py:dump", "external:json", "calls") in edges

    assert "symbol:pkg/same_a.py:process" in {node.id for node in graph.nodes}
    assert "symbol:pkg/same_b.py:process" in {node.id for node in graph.nodes}
    assert next(item for item in graph.nodes if item.id == "symbol:pkg/same_a.py:process").name == "process"

    codes = {item.code for item in graph.diagnostics}
    assert "syntax_error" in codes
    assert "dynamic_call" in codes
    assert not marker.exists()


def test_graph_is_json_serializable(tmp_path: Path) -> None:
    project = tmp_path / "project"
    _fixture(project, tmp_path / "executed.txt")
    graph = build_graph(project)
    rendered = json.dumps(graph.as_dict(), ensure_ascii=False)
    assert '"schema": "mokoa.code-graph/v1"' in rendered
    assert '"edges"' in rendered


def test_single_file_change_invalidates_old_edges(tmp_path: Path) -> None:
    project = tmp_path / "project"
    _fixture(project, tmp_path / "executed.txt")
    session = CodeGraphSession(project)
    graph = session.build()
    assert ("module:pkg/a.py", "module:pkg/b.py", "imports") in _edge_set(graph)

    _write(project, "pkg/a.py", """from .same_a import process

class Process:
    def run(self):
        return process()
""")
    refresh = session.refresh()

    assert "pkg/a.py" in refresh.changed_files
    removed = {(item.source, item.target, item.kind) for item in refresh.removed_edges}
    added = {(item.source, item.target, item.kind) for item in refresh.added_edges}
    assert ("module:pkg/a.py", "module:pkg/b.py", "imports") in removed
    assert ("symbol:pkg/a.py:Process.run", "symbol:pkg/b.py:helper", "calls") in removed
    assert ("module:pkg/a.py", "module:pkg/same_a.py", "imports") in added
    assert ("symbol:pkg/a.py:Process.run", "symbol:pkg/same_a.py:process", "calls") in added
    assert "pkg/b.py" not in refresh.changed_files


def test_refresh_without_changes_has_empty_diff(tmp_path: Path) -> None:
    project = tmp_path / "project"
    _fixture(project, tmp_path / "executed.txt")
    session = CodeGraphSession(project)
    session.build()
    refresh = session.refresh()
    assert refresh.changed_files == ()
    assert refresh.added_edges == ()
    assert refresh.removed_edges == ()