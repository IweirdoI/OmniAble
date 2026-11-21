"""Public CI checks for this repo's Clerk configuration; not a Clerk replacement."""

from __future__ import annotations

import csv
import hashlib
from pathlib import Path
import re
import sys
import tomllib


def check(root: Path) -> None:
    def rows(path: str) -> list[dict[str, str]]:
        with (root / path).open(encoding="utf-8", newline="") as stream:
            reader = csv.DictReader(stream)
            result = list(reader)
            if any(None in row or None in row.values() for row in result):
                raise ValueError(f"{path}: malformed CSV row")
            return result

    workspace = tomllib.loads((root / ".clerk/workspace.toml").read_text("utf-8"))
    if workspace["policy"]["auto_commit"] or workspace["policy"]["auto_push"]:
        raise ValueError("Clerk must keep manual PR-based Git finalization")
    if workspace["sync"]["enabled"]:
        raise ValueError("Fork-compatible configuration must not enable remote sync")
    for path in (root / ".clerk/config/kinds").glob("*.toml"):
        tomllib.loads(path.read_text("utf-8"))
    states = {row["key"] for row in rows(".clerk/config/registry/state.csv")}
    tasks = rows(".clerk/data/tasks.csv")
    seen: set[str] = set()
    issued = {(row["kind"], row["key"]) for row in rows(".clerk/seal/issued.csv")}
    for task in tasks:
        key = task["id"]
        if not re.fullmatch(r"TASK-\d{3,}", key) or key in seen:
            raise ValueError(f"Invalid or duplicate task ID: {key}")
        seen.add(key)
        if ("tasks", key) not in issued:
            raise ValueError(f"Task missing from issued ID ledger: {key}")
        if task["state"] not in states or not task["title"] or not task["accept"]:
            raise ValueError(f"Invalid state or missing task title/acceptance: {key}")
        if task["state"] == "done" and not (task["evidence"] and task["commit"]):
            raise ValueError(f"Completed task requires evidence and commit: {key}")
    seals = rows(".clerk/seal/seal.csv")
    required = {".clerk/data/tasks.csv", "docs/TEAM_STATUS.md#tasks"}
    keys = [row["key"] for row in seals]
    if len(set(keys)) != len(keys) or not required.issubset(keys):
        raise ValueError("Missing or duplicate task seals")
    for seal in seals:
        relative, _, region = seal["key"].partition("#")
        path = (root / relative).resolve()
        if not path.is_relative_to(root.resolve()):
            raise ValueError(f"Seal path outside repository: {relative}")
        content = path.read_bytes()
        if region:
            text = content.decode("utf-8")
            begin, end = f"<!-- clerk:begin {region} -->", f"<!-- clerk:end {region} -->"
            if text.count(begin) != 1 or text.count(end) != 1:
                raise ValueError(f"Missing or ambiguous region: {seal['key']}")
            content = text.split(begin, 1)[1].split(end, 1)[0].encode("utf-8")
        if hashlib.sha256(content).hexdigest() != seal["sha256"]:
            raise ValueError(f"Managed content differs from seal: {seal['key']}")


if __name__ == "__main__":
    try:
        check(Path(__file__).resolve().parents[1])
    except (ValueError, KeyError, OSError) as error:
        sys.exit(f"Clerk configuration check failed: {error}")
    print("Clerk configuration, task constraints and content hashes passed")
