"""Offline plugin package format, signing and verification regressions."""

from __future__ import annotations

import base64
import json
import zipfile
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from src.plugin_package import PluginPackageError, TrustStore, pack_plugin, verify_catalog, verify_plugin
from src.plugin_package.package import (
    MANIFEST_NAME,
    SIGNATURE_NAME,
    canonical_json,
    sha256_bytes,
)
from src.plugin_package.trust import TrustedKey

ROOT = Path(__file__).resolve().parents[1]
KEY_ID = "test-root"


def _keypair() -> tuple[Ed25519PrivateKey, TrustedKey]:
    private = Ed25519PrivateKey.generate()
    public = private.public_key()
    return private, TrustedKey(KEY_ID, "ed25519", public, "Test Publisher")


def _key_file(tmp_path: Path, private: Ed25519PrivateKey) -> Path:
    path = tmp_path / "test-key.pem"
    path.write_bytes(private.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ))
    return path


def _manifest(*, actions: list[dict] | None = None, dependencies: list[dict] | None = None) -> dict:
    return {
        "schema": "mokoa.plugin/v1",
        "id": "com.example.fixture",
        "name": "Fixture Plugin",
        "version": "1.2.3",
        "host_api": ">=1.0.0 <2.0.0",
        "entry": "payload/main.py",
        "license": "MIT",
        "license_file": "LICENSE",
        "publisher": {"key_id": KEY_ID, "name": "Test Publisher"},
        "capabilities": ["files.read"],
        "actions": actions or [
            {
                "id": "read",
                "name": "Read",
                "description": "Read a value.",
                "parameters": {"type": "object", "properties": {}},
                "capabilities": ["files.read"],
                "mutates": False,
            }
        ],
        "dependencies": dependencies or [],
        "files": {},
    }


def _source(tmp_path: Path, *, manifest: dict | None = None, payload: bytes = b"VALUE = 1\n") -> Path:
    source = tmp_path / "source"
    (source / "payload").mkdir(parents=True)
    (source / "manifest.json").write_text(json.dumps(manifest or _manifest(), ensure_ascii=False), encoding="utf-8")
    (source / "payload" / "main.py").write_bytes(payload)
    (source / "LICENSE").write_text("MIT fixture\n", encoding="utf-8")
    return source


def _trust(key: TrustedKey, revoked: set[str] | None = None) -> TrustStore:
    return TrustStore({key.key_id: key}, revoked or set())


def _trust_file(tmp_path: Path, key: TrustedKey, *, revoked: set[str] | None = None) -> tuple[Path, Path]:
    raw = key.public_key.public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    trust = {
        "schema": "mokoa.trust-root/v1",
        "keys": [{
            "key_id": key.key_id,
            "algorithm": "ed25519",
            "public_key": base64.b64encode(raw).decode("ascii"),
            "publisher": key.publisher,
        }],
    }
    trust_path = tmp_path / "trust.json"
    trust_path.write_text(json.dumps(trust), encoding="utf-8")
    revocation_path = tmp_path / "revocations.json"
    revocation_path.write_text(json.dumps({
        "schema": "mokoa.revocations/v1",
        "revoked": [{"key_id": item} for item in sorted(revoked or set())],
    }), encoding="utf-8")
    return trust_path, revocation_path


def _raw_package(
    path: Path,
    manifest: dict,
    files: dict[str, bytes],
    private: Ed25519PrivateKey,
    key_id: str = KEY_ID,
) -> Path:
    """Build a signed package without pack-time manifest validation."""

    files = dict(files)
    license_file = manifest.get("license_file")
    if isinstance(license_file, str) and license_file not in files:
        files[license_file] = b"MIT fixture\n"
    manifest = dict(manifest)
    publisher = dict(manifest.get("publisher", {}))
    publisher["key_id"] = key_id
    manifest["publisher"] = publisher
    manifest["files"] = {name: sha256_bytes(value) for name, value in files.items()}
    manifest_raw = canonical_json(manifest)
    signature_doc = {
        "schema": "mokoa.plugin-signature/v1",
        "algorithm": "ed25519",
        "key_id": key_id,
        "manifest_sha256": sha256_bytes(manifest_raw),
        "signature": base64.b64encode(private.sign(manifest_raw)).decode("ascii"),
    }
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(MANIFEST_NAME, manifest_raw)
        archive.writestr(SIGNATURE_NAME, canonical_json(signature_doc))
        for name, value in files.items():
            archive.writestr(name, value)
    return path


def _rewrite_zip(path: Path, replacements: dict[str, bytes]) -> None:
    with zipfile.ZipFile(path) as archive:
        entries = [(info.filename, archive.read(info)) for info in archive.infolist()]
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, value in entries:
            archive.writestr(name, replacements.get(name, value))


@pytest.mark.parametrize(("extra_name", "error"), [
    ("payload/NUL.txt", "unsafe_path"),
    ("payload/notes.", "unsafe_path"),
    ("payload/MAIN.py", "duplicate_path"),
    ("payload/main.py/notes.txt", "duplicate_path"),
])
def test_verifier_rejects_windows_path_conflicts(tmp_path, extra_name, error):
    private, key = _keypair()
    package = _raw_package(tmp_path / "paths.mko", _manifest(), {
        "payload/main.py": b"VALUE = 1\n",
        extra_name: b"sample text",
    }, private)
    with pytest.raises(PluginPackageError) as exc:
        verify_plugin(package, _trust(key), "1.4.0")
    assert exc.value.code == error


def test_package_digest_uses_the_verified_byte_snapshot(tmp_path, monkeypatch):
    private, key = _keypair()
    package = pack_plugin(_source(tmp_path), _key_file(tmp_path, private), tmp_path / "snapshot.mko")
    expected = sha256_bytes(package.read_bytes())
    real_open = Path.open
    reads = []

    def open_counted(path, *args, **kwargs):
        if path == package:
            reads.append(path)
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", open_counted)
    assert verify_plugin(package, _trust(key), "1.4.0").package_sha256 == expected
    assert reads == [package]


def test_sample_package_verifies_offline_with_local_trust_root() -> None:
    trust = TrustStore.load(
        ROOT / "plugins" / "trust" / "test-root.json",
        ROOT / "plugins" / "trust" / "revocations.json",
    )
    package = verify_plugin(ROOT / "plugins" / "samples" / "echo.mko", trust, "1.4.0")

    assert package.plugin_id == "com.mokoa.sample.echo"
    assert package.version == "1.0.0"
    assert package.key.key_id == "mokoa-test-root-2026"
    assert package.files == ("LICENSE", "payload/main.py")
    assert package.package_sha256.startswith("sha256:")


def test_pack_and_verify_round_trip(tmp_path: Path) -> None:
    private, key = _keypair()
    source = _source(tmp_path)
    output = tmp_path / "fixture.mko"
    pack_plugin(source, _key_file(tmp_path, private), output)

    package = verify_plugin(output, _trust(key), "1.9.0")
    assert package.manifest["entry"] == "payload/main.py"
    assert package.files == ("LICENSE", "payload/main.py")


def test_payload_tampering_is_rejected(tmp_path: Path) -> None:
    private, key = _keypair()
    output = tmp_path / "fixture.mko"
    pack_plugin(_source(tmp_path), _key_file(tmp_path, private), output)
    _rewrite_zip(output, {"payload/main.py": b"TAMPERED\n"})

    with pytest.raises(PluginPackageError) as captured:
        verify_plugin(output, _trust(key), "1.0.0")
    assert captured.value.code == "file_digest_mismatch"
    assert captured.value.path == "payload/main.py"


def test_manifest_tampering_is_rejected(tmp_path: Path) -> None:
    private, key = _keypair()
    output = tmp_path / "manifest-tamper.mko"
    pack_plugin(_source(tmp_path), _key_file(tmp_path, private), output)
    with zipfile.ZipFile(output) as archive:
        manifest_raw = archive.read(MANIFEST_NAME)
    _rewrite_zip(output, {MANIFEST_NAME: manifest_raw.replace(b"Fixture Plugin", b"Tampered Plugin")})

    with pytest.raises(PluginPackageError) as captured:
        verify_plugin(output, _trust(key), "1.0.0")
    assert captured.value.code == "manifest_digest_mismatch"


def test_unknown_signer_is_rejected(tmp_path: Path) -> None:
    private, key = _keypair()
    output = _raw_package(
        tmp_path / "unknown.mko",
        _manifest(),
        {"payload/main.py": b"VALUE = 1\n"},
        private,
        key_id="other-root",
    )

    with pytest.raises(PluginPackageError) as captured:
        verify_plugin(output, _trust(key), "1.0.0")
    assert captured.value.code == "unknown_signer"


def test_revoked_signer_is_rejected(tmp_path: Path) -> None:
    private, key = _keypair()
    output = _raw_package(tmp_path / "revoked.mko", _manifest(), {"payload/main.py": b"VALUE = 1\n"}, private)
    with pytest.raises(PluginPackageError) as captured:
        verify_plugin(output, _trust(key, {KEY_ID}), "1.0.0")
    assert captured.value.code == "revoked_signer"


def test_incompatible_host_api_is_rejected(tmp_path: Path) -> None:
    private, key = _keypair()
    output = _raw_package(tmp_path / "host.mko", _manifest(), {"payload/main.py": b"VALUE = 1\n"}, private)
    with pytest.raises(PluginPackageError) as captured:
        verify_plugin(output, _trust(key), "2.0.0")
    assert captured.value.code == "incompatible_host_api"


def test_duplicate_action_is_rejected(tmp_path: Path) -> None:
    private, key = _keypair()
    action = {
        "id": "read",
        "name": "Read",
        "description": "Read a value.",
        "parameters": {"type": "object", "properties": {}},
        "capabilities": ["files.read"],
    }
    output = _raw_package(
        tmp_path / "duplicate-action.mko",
        _manifest(actions=[action, dict(action)]),
        {"payload/main.py": b"VALUE = 1\n"},
        private,
    )
    with pytest.raises(PluginPackageError) as captured:
        verify_plugin(output, _trust(key), "1.0.0")
    assert captured.value.code == "duplicate_action"
    assert captured.value.path == "actions[1].id"


def test_unsafe_zip_path_is_rejected(tmp_path: Path) -> None:
    private, key = _keypair()
    output = tmp_path / "unsafe.mko"
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr(MANIFEST_NAME, b"{}")
        archive.writestr(SIGNATURE_NAME, b"{}")
        archive.writestr("../escape.txt", b"x")
    with pytest.raises(PluginPackageError) as captured:
        verify_plugin(output, _trust(key), "1.0.0")
    assert captured.value.code == "unsafe_path"
    assert captured.value.path == "../escape.txt"


def test_duplicate_zip_path_is_rejected(tmp_path: Path) -> None:
    private, key = _keypair()
    output = tmp_path / "duplicate.mko"
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("payload/a.txt", b"a")
        archive.writestr("payload/a.txt", b"b")
    with pytest.raises(PluginPackageError) as captured:
        verify_plugin(output, _trust(key), "1.0.0")
    assert captured.value.code == "duplicate_path"
    assert captured.value.path == "payload/a.txt"


def test_missing_dependency_is_rejected(tmp_path: Path) -> None:
    private, key = _keypair()
    manifest = _manifest(dependencies=[{"id": "com.example.base", "version": "1.0.0"}])
    output = _raw_package(tmp_path / "missing.mko", manifest, {"payload/main.py": b"VALUE = 1\n"}, private)
    with pytest.raises(PluginPackageError) as captured:
        verify_plugin(output, _trust(key), "1.0.0", catalog=[])
    assert captured.value.code == "missing_dependency"


def test_duplicate_plugin_ids_are_rejected(tmp_path: Path) -> None:
    private, key = _keypair()
    directory = tmp_path / "catalog"
    directory.mkdir()
    for name in ("first.mko", "second.mko"):
        pack_plugin(_source(tmp_path / name), _key_file(tmp_path, private), directory / name)
    with pytest.raises(PluginPackageError) as captured:
        verify_catalog(directory, _trust(key), "1.0.0")
    assert captured.value.code == "duplicate_plugin_id"


def test_duplicate_plugin_names_are_rejected(tmp_path: Path) -> None:
    private, key = _keypair()
    directory = tmp_path / "catalog"
    directory.mkdir()
    first = _manifest()
    first["id"] = "com.example.first"
    second = _manifest()
    second["id"] = "com.example.second"
    key_path = _key_file(tmp_path, private)
    pack_plugin(_source(tmp_path / "first-source", manifest=first), key_path, directory / "first.mko")
    pack_plugin(_source(tmp_path / "second-source", manifest=second), key_path, directory / "second.mko")
    with pytest.raises(PluginPackageError) as captured:
        verify_catalog(directory, _trust(key), "1.0.0")
    assert captured.value.code == "duplicate_plugin_name"


def test_catalog_dependency_lock_and_package_digest(tmp_path: Path) -> None:
    private, key = _keypair()
    catalog = tmp_path / "catalog"
    catalog.mkdir()
    key_path = _key_file(tmp_path, private)

    base_manifest = _manifest()
    base_manifest["id"] = "com.example.base"
    base_manifest["name"] = "Base Plugin"
    base_manifest["version"] = "1.0.0"
    base_output = catalog / "base.mko"
    pack_plugin(_source(tmp_path / "base-source", manifest=base_manifest), key_path, base_output)
    base = verify_plugin(base_output, _trust(key), "1.0.0")

    target_manifest = _manifest(dependencies=[{
        "id": "com.example.base",
        "version": "1.0.0",
        "digest": base.package_sha256,
    }])
    pack_plugin(_source(tmp_path / "target-source", manifest=target_manifest), key_path, catalog / "target.mko")

    packages = verify_catalog(catalog, _trust(key), "1.0.0")
    assert {item.plugin_id for item in packages} == {"com.example.base", "com.example.fixture"}


def test_verifier_does_not_execute_payload(tmp_path: Path) -> None:
    private, key = _keypair()
    marker = tmp_path / "executed.txt"
    source = _source(tmp_path, payload=f"open({str(marker)!r}, 'w').write('ran')\n".encode())
    output = tmp_path / "no-exec.mko"
    pack_plugin(source, _key_file(tmp_path, private), output)
    verify_plugin(output, _trust(key), "1.0.0")
    assert not marker.exists()


def test_trust_file_and_revocation_file_drive_verification(tmp_path: Path) -> None:
    private, key = _keypair()
    output = _raw_package(tmp_path / "trust.mko", _manifest(), {"payload/main.py": b"VALUE = 1\n"}, private)
    trust_path, revocations = _trust_file(tmp_path, key)
    assert verify_plugin(output, TrustStore.load(trust_path, revocations), "1.0.0").plugin_id == "com.example.fixture"

    trust_path, revocations = _trust_file(tmp_path, key, revoked={KEY_ID})
    with pytest.raises(PluginPackageError) as captured:
        verify_plugin(output, TrustStore.load(trust_path, revocations), "1.0.0")
    assert captured.value.code == "revoked_signer"
