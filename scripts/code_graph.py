"""Read-only CLI for the TASK-032 static Python code graph."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.code_graph import CodeGraphSession


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="MOKOA read-only Python code graph")
    parser.add_argument("--root", default=str(ROOT), help="project directory to parse")
    parser.add_argument("--out", default=None, help="optional JSON output path")
    parser.add_argument("--max-files", type=int, default=5000)
    parser.add_argument("--refresh", action="store_true", help="build twice and report changed edges")
    args = parser.parse_args(argv)

    session = CodeGraphSession(args.root, max_files=args.max_files)
    graph = session.build()
    payload: dict = graph.as_dict()
    if args.refresh:
        refresh = session.refresh()
        payload["refresh"] = {
            "changed_files": list(refresh.changed_files),
            "added_edges": [item.as_dict() for item in refresh.added_edges],
            "removed_edges": [item.as_dict() for item in refresh.removed_edges],
        }
    rendered = json.dumps(payload, ensure_ascii=False, indent=2)
    if args.out:
        Path(args.out).write_text(rendered + "\n", encoding="utf-8")
        print(json.dumps({"ok": True, "out": args.out, "nodes": len(graph.nodes), "edges": len(graph.edges)}, ensure_ascii=False))
    else:
        print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())