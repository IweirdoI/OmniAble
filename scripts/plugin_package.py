"""CLI for packing and offline verification of MOKOA plugin packages.

Examples:
    python scripts/plugin_package.py pack plugins/samples/echo --key test-key.pem --out echo.mko
    python scripts/plugin_package.py verify echo.mko --trust plugins/trust/test-root.json \
        --revocations plugins/trust/revocations.json --host-version 1.4.0
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.plugin_package import PluginPackageError, TrustStore, pack_plugin, verify_catalog, verify_plugin


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="MOKOA offline plugin package tool")
    sub = parser.add_subparsers(dest="command", required=True)

    pack = sub.add_parser("pack", help="build and sign a package from a source directory")
    pack.add_argument("source", help="source directory containing manifest.json, payload/ and LICENSE")
    pack.add_argument("--key", required=True, help="Ed25519 private key PEM")
    pack.add_argument("--out", required=True, help="output .mko path")

    verify = sub.add_parser("verify", help="verify one package without running it")
    verify.add_argument("package", help=".mko package")
    verify.add_argument("--trust", required=True, help="local trust-root JSON")
    verify.add_argument("--revocations", help="local offline revocation JSON")
    verify.add_argument("--host-version", required=True, help="current host API SemVer")
    verify.add_argument("--catalog", help="directory of .mko packages for dependency and duplicate-ID checks")

    catalog = sub.add_parser("verify-catalog", help="verify all packages in a directory and detect duplicate IDs")
    catalog.add_argument("directory", help="directory containing .mko packages")
    catalog.add_argument("--trust", required=True, help="local trust-root JSON")
    catalog.add_argument("--revocations", help="local offline revocation JSON")
    catalog.add_argument("--host-version", required=True, help="current host API SemVer")
    return parser


def _summary(package) -> dict:
    return {
        "ok": True,
        "id": package.plugin_id,
        "version": package.version,
        "host_api": package.manifest["host_api"],
        "key_id": package.key.key_id,
        "publisher": package.key.publisher,
        "files": list(package.files),
        "package_sha256": package.package_sha256,
    }


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "pack":
            result = pack_plugin(args.source, args.key, args.out)
            manifest_path = Path(args.source) / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            print(json.dumps({"ok": True, "path": str(result), "id": manifest.get("id"), "version": manifest.get("version")}, ensure_ascii=False))
            return 0

        trust = TrustStore.load(args.trust, args.revocations)
        if args.command == "verify":
            catalog = None
            if args.catalog:
                catalog = verify_catalog(args.catalog, trust, args.host_version)
            package = verify_plugin(args.package, trust, args.host_version, catalog=catalog)
            print(json.dumps(_summary(package), ensure_ascii=False, indent=2))
            return 0

        packages = verify_catalog(args.directory, trust, args.host_version)
        print(json.dumps({"ok": True, "count": len(packages), "packages": [_summary(item) for item in packages]}, ensure_ascii=False, indent=2))
        return 0
    except PluginPackageError as error:
        print(json.dumps({"ok": False, **error.to_dict()}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())