"""Offline MOKOA plugin package format, signing and verification.

The package format is intentionally data-only: verification reads, hashes and
validates bytes; it never imports or executes package code.
"""

from .errors import PluginPackageError
from .package import (
    PACKAGE_SCHEMA,
    SIGNATURE_SCHEMA,
    VerifiedPackage,
    pack_plugin,
    verify_catalog,
    verify_plugin,
)
from .trust import TrustStore

__all__ = [
    "PACKAGE_SCHEMA",
    "SIGNATURE_SCHEMA",
    "PluginPackageError",
    "TrustStore",
    "VerifiedPackage",
    "pack_plugin",
    "verify_catalog",
    "verify_plugin",
]