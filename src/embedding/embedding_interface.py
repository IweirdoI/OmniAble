"""Unified embedding interface. Module: core."""

from __future__ import annotations

import os
import threading
from functools import lru_cache
from pathlib import Path

import yaml

from src.common.interfaces import EmbeddingProvider

_SHARED_PROVIDERS: dict[str, EmbeddingProvider] = {}
_SHARED_LOCK = threading.Lock()


@lru_cache(maxsize=1)
def load_embedding_config() -> dict:
    config_path = Path(__file__).resolve().parents[2] / "configs" / "default.yaml"
    if not config_path.exists():
        return {}
    with config_path.open(encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    embedding_cfg = data.get("embedding", {})
    return embedding_cfg if isinstance(embedding_cfg, dict) else {}


def resolve_embedding_backend_name(backend: str | None = None) -> str:
    """Resolve auto/env/config to a concrete backend name."""

    cfg = load_embedding_config()
    resolved = (
        backend
        or os.getenv("OS_AGENT_EMBEDDING_BACKEND")
        or os.getenv("EMBEDDING_BACKEND")
        or str(cfg.get("backend", "auto"))
    ).strip().lower()
    if resolved == "auto":
        resolved = "local"
    return resolved


def _build_embedding_backend(resolved: str) -> EmbeddingProvider:
    cfg = load_embedding_config()
    if resolved == "hash":
        from src.embedding.local_embedding import HashEmbeddingAdapter

        return HashEmbeddingAdapter(
            dimension=int(os.getenv("MEMORY_VECTOR_DIM", "1024"))
        )

    if resolved != "local":
        raise ValueError(f"不支持的 embedding backend: {resolved!r}")

    from src.embedding.local_embedding import LocalEmbeddingAdapter

    device = cfg.get("device")
    return LocalEmbeddingAdapter(
        model_name=str(cfg.get("local_model", "BAAI/bge-m3")),
        device=str(device) if device else None,
        normalize=bool(cfg.get("normalize", True)),
        allow_stub_fallback=bool(cfg.get("allow_stub_fallback", True)),
    )


def create_embedding_backend(backend: str | None = None) -> EmbeddingProvider:
    """
    Factory for pluggable embedding backends. Returns a new instance.

    auto   -> local on Windows/dev
    local  -> BGE-M3 / sentence-transformers for dev
    hash   -> deterministic offline vectors for tests/wiring only

    Request paths should use ``get_shared_embedding_backend`` so local model
    sessions are not created and destroyed per /store-entry.
    """
    return _build_embedding_backend(resolve_embedding_backend_name(backend))


def get_shared_embedding_backend(backend: str | None = None) -> EmbeddingProvider:
    """Reuse a single embedding provider per backend in the process."""

    resolved = resolve_embedding_backend_name(backend)
    with _SHARED_LOCK:
        provider = _SHARED_PROVIDERS.get(resolved)
        if provider is None:
            provider = _build_embedding_backend(resolved)
            _SHARED_PROVIDERS[resolved] = provider
        return provider


def reset_shared_embedding_backends() -> None:
    """Drop cached providers. Tests only; closes adapters that expose close()."""

    with _SHARED_LOCK:
        providers = list(_SHARED_PROVIDERS.values())
        _SHARED_PROVIDERS.clear()
    for provider in providers:
        closer = getattr(provider, "close", None)
        if not callable(closer):
            continue
        try:
            closer()
        except Exception:
            pass
