"""Read-only static Python dependency and symbol graph.

This module parses source bytes with :mod:`ast`; it never imports, executes or
writes target project code. It is the parser core for TASK-032, not yet a
TASK-024 host plugin.
"""

from .graph import (
    CodeGraph,
    CodeGraphSession,
    Diagnostic,
    Edge,
    GraphRefresh,
    Node,
    build_graph,
)

__all__ = [
    "CodeGraph",
    "CodeGraphSession",
    "Diagnostic",
    "Edge",
    "GraphRefresh",
    "Node",
    "build_graph",
]