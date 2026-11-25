"""Fixture plugin payload. The package verifier must never import this file."""

from __future__ import annotations


def echo(text: str) -> str:
    """Return text unchanged; this is sample data, not an execution path."""

    return text