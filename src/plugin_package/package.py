"""Offline pack/verify implementation for the MOKOA plugin package contract.

A package is a deterministic ZIP containing:

* ``manifest.json`` — immutable package metadata, action catalog, capabilities,
  dependency locks and payload file digests;
* ``signature.json`` — Ed25519 signature over the canonical manifest;
* ``payload/...`` — data/code owned by the plugin (never imported by verifier);
* ``LICENSE`` — optional license text referenced by the manifest.

The verifier reads bytes and hashes only. It never imports, evaluates, extracts
to disk, or executes package content.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
import stat
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from .errors import PluginPackageError
from .semver import SemVer, VersionRange
from .trust import TrustStore, TrustedKey

PACKAGE_SCHEMA = "mokoa.plugin/v1"
SIGNATURE_SCHEMA = "mokoa.plugin-signature/v1"
PACKAGE_SUFFIX = ".mko"
MANIFEST_NAME = "manifest.json"
SIGNATURE_NAME = "signature.json"
CONTROL_FILES = {MANIFEST_NAME, SIGNATURE_NAME}
ALLOWED_CAPABILITIES = {
    "files.read",
    "files.write",
    "network.local",
    "network.internet",
    "process.spawn",
    "desktop.uia",
    "clipboard.read",
    "clipboard.write",
    "memory.read",
    "memory.write",
}
MAX_PACKAGE_BYTES = 16 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 32 * 1024 * 1024
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_FILES = 512
_ID = re.compile(r"^[a-z0-9]+(?:[._-][a-z0-9]+)*$")
_ACTION_ID = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_KEY_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{2,127}$")


@dataclass(frozen=True)
class VerifiedPackage:
    """A package whose manifest, signature, files and dependencies passed."""

    path: Path
    manifest: dict[str, Any]
    package_sha256: str
    key: TrustedKey
    signature: dict[str, Any]
    files: tuple[str, ...]

    @property
    def plugin_id(self) -> str:
        return str(self.manifest["id"])

    @property
    def version(self) -> str:
        return str(self.manifest["version"])


def canonical_json(value: Any) -> bytes:
    """Canonical UTF-8 JSON used for signatures and manifest hashes."""

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _json_no_duplicates(raw: bytes, *, code: str, label: str) -> Any:
    def pairs_hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise PluginPackageError(code, f"{label} 重复字段 {key!r}", path=key)
            result[key] = value
        return result

    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=pairs_hook)
    except UnicodeDecodeError as exc:
        raise PluginPackageError(code, f"{label} 不是 UTF-8") from exc
    except json.JSONDecodeError as exc:
        raise PluginPackageError(code, f"{label} 不是有效 JSON: {exc}") from exc


def _safe_path(
    value: Any,
    *,
    field: str,
    allow_license: bool = False,
    allow_control: bool = False,
    allow_directory: bool = False,
) -> str:
    if not isinstance(value, str) or not value:
        raise PluginPackageError("unsafe_path", "路径必须是非空字符串", path=field)
    if "\\" in value or "\x00" in value or value.startswith("/"):
        raise PluginPackageError("unsafe_path", f"路径包含不允许的字符: {value!r}", path=field)
    if re.match(r"^[A-Za-z]:", value):
        raise PluginPackageError("unsafe_path", f"不允许盘符路径: {value!r}", path=field)
    path = Path(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise PluginPackageError("unsafe_path", f"路径必须位于包内: {value!r}", path=field)
    normalized = path.as_posix()
    if normalized != value:
        raise PluginPackageError("unsafe_path", f"路径必须使用规范的 / 分隔: {value!r}", path=field)
    if allow_directory and (value == "payload" or value.startswith("payload/")):
        return value
    if value in CONTROL_FILES:
        if allow_control:
            return value
        raise PluginPackageError("unsafe_path", "控制文件不能作为普通 payload", path=field)
    if value == "LICENSE" and allow_license:
        return value
    if not value.startswith("payload/") or value == "payload/":
        raise PluginPackageError(
            "unsafe_path",
            "普通文件必须位于 payload/ 下；许可证只能是 LICENSE",
            path=field,
        )
    return value


def _expect_object(value: Any, *, field: str, code: str = "invalid_manifest") -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PluginPackageError(code, "必须是 JSON 对象", path=field)
    return value


def _expect_string(value: Any, *, field: str, code: str = "invalid_manifest", nonempty: bool = True) -> str:
    if not isinstance(value, str) or (nonempty and not value.strip()):
        raise PluginPackageError(code, "必须是非空字符串", path=field)
    return value


def _expect_string_list(value: Any, *, field: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise PluginPackageError("invalid_manifest", "必须是字符串数组", path=field)
    return value


def _validate_manifest(manifest: dict[str, Any], *, source_files: set[str] | None = None) -> None:
    schema = manifest.get("schema")
    if schema != PACKAGE_SCHEMA:
        raise PluginPackageError("invalid_schema", f"manifest.schema 必须是 {PACKAGE_SCHEMA}", path="schema")

    plugin_id = _expect_string(manifest.get("id"), field="id")
    if not _ID.fullmatch(plugin_id):
        raise PluginPackageError("invalid_manifest", f"插件 ID 格式无效: {plugin_id!r}", path="id")
    _expect_string(manifest.get("name"), field="name")
    SemVer.parse(_expect_string(manifest.get("version"), field="version"), field="version")

    host_api = _expect_string(manifest.get("host_api"), field="host_api")
    VersionRange.parse(host_api, field="host_api")

    publisher = _expect_object(manifest.get("publisher"), field="publisher")
    key_id = _expect_string(publisher.get("key_id"), field="publisher.key_id")
    if not _KEY_ID.fullmatch(key_id):
        raise PluginPackageError("invalid_manifest", "publisher.key_id 格式无效", path="publisher.key_id")
    _expect_string(publisher.get("name"), field="publisher.name")

    license_value = manifest.get("license")
    if not isinstance(license_value, str) or not license_value.strip():
        raise PluginPackageError("invalid_manifest", "license 必须是非空 SPDX 标识或名称", path="license")
    license_file = manifest.get("license_file")
    if license_file is not None:
        _safe_path(license_file, field="license_file", allow_license=True)

    capabilities = _expect_string_list(manifest.get("capabilities"), field="capabilities")
    if len(set(capabilities)) != len(capabilities):
        raise PluginPackageError("duplicate_capability", "capabilities 不能重复", path="capabilities")
    unknown = sorted(set(capabilities) - ALLOWED_CAPABILITIES)
    if unknown:
        raise PluginPackageError("invalid_capability", f"未声明的能力: {', '.join(unknown)}", path="capabilities")

    entry = _safe_path(manifest.get("entry"), field="entry")
    actions = manifest.get("actions")
    if not isinstance(actions, list) or not actions:
        raise PluginPackageError("invalid_manifest", "actions 必须是非空数组", path="actions")
    action_ids: set[str] = set()
    for index, action in enumerate(actions):
        field = f"actions[{index}]"
        action = _expect_object(action, field=field)
        action_id = _expect_string(action.get("id"), field=f"{field}.id")
        if not _ACTION_ID.fullmatch(action_id):
            raise PluginPackageError("invalid_action", "动作 ID 格式无效", path=f"{field}.id")
        if action_id in action_ids:
            raise PluginPackageError("duplicate_action", f"动作 ID 重复: {action_id}", path=f"{field}.id")
        action_ids.add(action_id)
        _expect_string(action.get("name"), field=f"{field}.name")
        _expect_string(action.get("description"), field=f"{field}.description")
        parameters = action.get("parameters")
        if not isinstance(parameters, dict) or parameters.get("type") != "object":
            raise PluginPackageError("invalid_action", "parameters 必须是 type=object 的 JSON Schema", path=f"{field}.parameters")
        action_capabilities = action.get("capabilities", [])
        if not isinstance(action_capabilities, list) or not all(isinstance(item, str) for item in action_capabilities):
            raise PluginPackageError("invalid_action", "action.capabilities 必须是字符串数组", path=f"{field}.capabilities")
        if len(set(action_capabilities)) != len(action_capabilities):
            raise PluginPackageError("duplicate_capability", "action.capabilities 不能重复", path=f"{field}.capabilities")
        unknown_action_caps = sorted(set(action_capabilities) - set(capabilities))
        if unknown_action_caps:
            raise PluginPackageError(
                "invalid_capability",
                f"动作能力未在包级声明: {', '.join(unknown_action_caps)}",
                path=f"{field}.capabilities",
            )
        if "mutates" in action and not isinstance(action["mutates"], bool):
            raise PluginPackageError("invalid_action", "mutates 必须是布尔值", path=f"{field}.mutates")

    dependencies = manifest.get("dependencies", [])
    if not isinstance(dependencies, list):
        raise PluginPackageError("invalid_manifest", "dependencies 必须是数组", path="dependencies")
    dependency_ids: set[str] = set()
    for index, dependency in enumerate(dependencies):
        field = f"dependencies[{index}]"
        dependency = _expect_object(dependency, field=field)
        dependency_id = _expect_string(dependency.get("id"), field=f"{field}.id")
        if not _ID.fullmatch(dependency_id):
            raise PluginPackageError("invalid_dependency", "依赖 ID 格式无效", path=f"{field}.id")
        if dependency_id == plugin_id:
            raise PluginPackageError("invalid_dependency", "插件不能依赖自身", path=f"{field}.id")
        if dependency_id in dependency_ids:
            raise PluginPackageError("duplicate_dependency", f"依赖 ID 重复: {dependency_id}", path=f"{field}.id")
        dependency_ids.add(dependency_id)
        SemVer.parse(_expect_string(dependency.get("version"), field=f"{field}.version"), field=f"{field}.version")
        digest = dependency.get("digest")
        if digest is not None and (not isinstance(digest, str) or not _DIGEST.fullmatch(digest)):
            raise PluginPackageError("invalid_dependency", "依赖 digest 必须是 sha256:<hex>", path=f"{field}.digest")

    files = manifest.get("files")
    if not isinstance(files, dict):
        raise PluginPackageError("invalid_manifest", "files 必须是对象", path="files")
    file_paths: set[str] = set()
    for raw_path, digest in files.items():
        path = _safe_path(raw_path, field=f"files[{raw_path}]", allow_license=True)
        if path in file_paths:
            raise PluginPackageError("duplicate_path", f"files 重复路径: {path}", path=f"files[{path}]")
        file_paths.add(path)
        if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
            raise PluginPackageError("invalid_manifest", "文件摘要必须是 sha256:<hex>", path=f"files[{path}]")
    if entry not in file_paths:
        raise PluginPackageError("missing_file", f"入口文件未列入 files: {entry}", path="entry")
    if license_file is not None and license_file not in file_paths:
        raise PluginPackageError("missing_file", f"许可证文件未列入 files: {license_file}", path="license_file")
    if source_files is not None and file_paths != source_files:
        missing = sorted(source_files - file_paths)
        extra = sorted(file_paths - source_files)
        if missing:
            raise PluginPackageError("missing_file", f"未列入 files 的文件: {', '.join(missing)}", path="files")
        if extra:
            raise PluginPackageError("extra_file", f"files 中不存在于包内的文件: {', '.join(extra)}", path="files")


def _load_private_key(path: Path) -> Ed25519PrivateKey:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise PluginPackageError("invalid_signing_key", f"无法读取签名私钥 {path}: {exc}", path=str(path)) from exc
    try:
        key = serialization.load_pem_private_key(raw, password=None)
    except (ValueError, TypeError) as exc:
        raise PluginPackageError("invalid_signing_key", f"私钥不是有效的 PEM: {exc}", path=str(path)) from exc
    if not isinstance(key, Ed25519PrivateKey):
        raise PluginPackageError("invalid_signing_key", "只支持 Ed25519 私钥", path=str(path))
    return key


def _signature_document(key_id: str, manifest_raw: bytes, signature: bytes) -> dict[str, Any]:
    return {
        "schema": SIGNATURE_SCHEMA,
        "algorithm": "ed25519",
        "key_id": key_id,
        "manifest_sha256": sha256_bytes(manifest_raw),
        "signature": base64.b64encode(signature).decode("ascii"),
    }


def _zip_add(zf: zipfile.ZipFile, name: str, data: bytes) -> None:
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = (stat.S_IFREG | 0o644) << 16
    zf.writestr(info, data)


def _collect_source_files(source: Path) -> dict[str, bytes]:
    if not source.is_dir():
        raise PluginPackageError("invalid_source", f"源目录不存在: {source}", path=str(source))
    files: dict[str, bytes] = {}
    for path in sorted(source.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(source).as_posix()
        if relative == MANIFEST_NAME:
            continue
        safe = _safe_path(relative, field=relative, allow_license=True)
        if path.stat().st_size > MAX_FILE_BYTES:
            raise PluginPackageError("file_too_large", f"文件超过 {MAX_FILE_BYTES} 字节", path=safe)
        if safe in files:
            raise PluginPackageError("duplicate_path", f"源目录重复文件: {safe}", path=safe)
        files[safe] = path.read_bytes()
        if len(files) > MAX_FILES:
            raise PluginPackageError("too_many_files", f"文件数超过 {MAX_FILES}", path=safe)
    if not files:
        raise PluginPackageError("missing_file", "源目录没有 payload 文件")
    return files


def pack_plugin(source: str | Path, private_key: str | Path, output: str | Path) -> Path:
    """Pack a source directory into a signed ``.mko`` file.

    The source manifest must include publisher.key_id; private_key is read only
    for this operation and is never copied into the package.
    """

    source = Path(source)
    output = Path(output)
    manifest_path = source / MANIFEST_NAME
    try:
        manifest_raw = manifest_path.read_bytes()
    except OSError as exc:
        raise PluginPackageError("missing_manifest", f"缺少 {MANIFEST_NAME}", path=str(manifest_path)) from exc
    manifest = _expect_object(_json_no_duplicates(manifest_raw, code="invalid_manifest", label=MANIFEST_NAME), field=MANIFEST_NAME)
    # The source manifest may leave files empty; pack replaces it with the
    # computed digest map before validation and signing.
    source_files = _collect_source_files(source)
    manifest = dict(manifest)
    manifest["files"] = {name: sha256_bytes(data) for name, data in sorted(source_files.items())}
    _validate_manifest(manifest, source_files=set(source_files))
    key_id = str(manifest["publisher"]["key_id"])
    key = _load_private_key(Path(private_key))
    signed_manifest = canonical_json(manifest)
    signature = key.sign(signed_manifest)
    signature_doc = _signature_document(key_id, signed_manifest, signature)
    signature_raw = canonical_json(signature_doc)

    output.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=output.name + ".", suffix=".tmp", dir=output.parent)
    os.close(fd)
    temp_path = Path(temp_name)
    try:
        with zipfile.ZipFile(temp_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
            _zip_add(zf, MANIFEST_NAME, signed_manifest)
            _zip_add(zf, SIGNATURE_NAME, signature_raw)
            for name, data in sorted(source_files.items()):
                _zip_add(zf, name, data)
        if temp_path.stat().st_size > MAX_PACKAGE_BYTES:
            raise PluginPackageError("package_too_large", f"包超过 {MAX_PACKAGE_BYTES} 字节", path=str(output))
        os.replace(temp_path, output)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise
    return output


def _read_zip(path: Path) -> tuple[bytes, bytes, dict[str, bytes]]:
    if not path.is_file():
        raise PluginPackageError("missing_package", f"包不存在: {path}", path=str(path))
    if path.stat().st_size > MAX_PACKAGE_BYTES:
        raise PluginPackageError("package_too_large", f"包超过 {MAX_PACKAGE_BYTES} 字节", path=str(path))
    try:
        archive = zipfile.ZipFile(path)
    except (OSError, zipfile.BadZipFile) as exc:
        raise PluginPackageError("invalid_zip", f"不是有效 ZIP: {exc}", path=str(path)) from exc
    with archive:
        infos = archive.infolist()
        names: set[str] = set()
        data: dict[str, bytes] = {}
        total = 0
        for info in infos:
            name = info.filename
            if info.is_dir():
                _safe_path(name.rstrip("/"), field=name, allow_directory=True)
                continue
            safe = _safe_path(name, field=name, allow_license=True, allow_control=True)
            if safe in names:
                raise PluginPackageError("duplicate_path", f"ZIP 重复路径: {safe}", path=safe)
            names.add(safe)
            if len(names) > MAX_FILES + len(CONTROL_FILES):
                raise PluginPackageError("too_many_files", f"文件数超过 {MAX_FILES}", path=safe)
            if info.file_size > MAX_FILE_BYTES:
                raise PluginPackageError("file_too_large", f"文件超过 {MAX_FILE_BYTES} 字节", path=safe)
            total += info.file_size
            if total > MAX_UNCOMPRESSED_BYTES:
                raise PluginPackageError("package_too_large", f"解压后超过 {MAX_UNCOMPRESSED_BYTES} 字节", path=str(path))
            mode = info.external_attr >> 16
            if stat.S_ISLNK(mode):
                raise PluginPackageError("unsafe_path", "包内不允许符号链接", path=safe)
            data[safe] = archive.read(info)
        if MANIFEST_NAME not in data:
            raise PluginPackageError("missing_manifest", "包内缺少 manifest.json", path=MANIFEST_NAME)
        if SIGNATURE_NAME not in data:
            raise PluginPackageError("missing_signature", "包内缺少 signature.json", path=SIGNATURE_NAME)
        return data[MANIFEST_NAME], data[SIGNATURE_NAME], {
            name: value for name, value in data.items() if name not in CONTROL_FILES
        }


def _validate_signature(
    manifest_raw: bytes,
    manifest: dict[str, Any],
    signature_raw: bytes,
    trust: TrustStore,
) -> tuple[TrustedKey, dict[str, Any]]:
    signature = _expect_object(
        _json_no_duplicates(signature_raw, code="invalid_signature", label=SIGNATURE_NAME),
        field=SIGNATURE_NAME,
        code="invalid_signature",
    )
    if signature.get("schema") != SIGNATURE_SCHEMA:
        raise PluginPackageError("invalid_signature", f"signature.schema 必须是 {SIGNATURE_SCHEMA}", path="signature.schema")
    if signature.get("algorithm") != "ed25519":
        raise PluginPackageError("invalid_signature", "只支持 ed25519 签名", path="signature.algorithm")
    key_id = _expect_string(signature.get("key_id"), field="signature.key_id", code="invalid_signature")
    publisher = _expect_object(manifest.get("publisher"), field="publisher")
    if publisher.get("key_id") != key_id:
        raise PluginPackageError("invalid_signature", "manifest.publisher.key_id 与 signature.key_id 不一致", path="signature.key_id")
    manifest_digest = signature.get("manifest_sha256")
    if manifest_digest != sha256_bytes(manifest_raw):
        raise PluginPackageError("manifest_digest_mismatch", "manifest 字节与签名哈希不一致", path="signature.manifest_sha256")
    encoded = signature.get("signature")
    if not isinstance(encoded, str):
        raise PluginPackageError("invalid_signature", "signature.signature 必须是 base64 字符串", path="signature.signature")
    try:
        signature_bytes = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise PluginPackageError("invalid_signature", "signature.signature 不是有效 base64", path="signature.signature") from exc
    key = trust.verify(key_id, signature_bytes, canonical_json(manifest))
    return key, signature


def verify_plugin(
    package: str | Path,
    trust: TrustStore,
    host_version: str,
    *,
    catalog: Sequence[VerifiedPackage] | None = None,
) -> VerifiedPackage:
    """Verify one package without running any of its code."""

    package = Path(package)
    manifest_raw, signature_raw, files = _read_zip(package)
    manifest = _expect_object(
        _json_no_duplicates(manifest_raw, code="invalid_manifest", label=MANIFEST_NAME),
        field=MANIFEST_NAME,
    )
    _validate_manifest(manifest)
    manifest_files = manifest["files"]
    missing = sorted(set(manifest_files) - set(files))
    extra = sorted(set(files) - set(manifest_files))
    if missing:
        raise PluginPackageError("missing_file", f"包内缺少文件: {', '.join(missing)}", path=missing[0])
    if extra:
        raise PluginPackageError("extra_file", f"包内存在未声明文件: {', '.join(extra)}", path=extra[0])
    for name, expected in manifest_files.items():
        actual = sha256_bytes(files[name])
        if actual != expected:
            raise PluginPackageError("file_digest_mismatch", "文件摘要与 manifest 不一致", path=name)

    key, signature = _validate_signature(manifest_raw, manifest, signature_raw, trust)

    version = SemVer.parse(str(manifest["version"]), field="version")
    if not VersionRange.parse(str(manifest["host_api"]), field="host_api").contains(SemVer.parse(host_version, field="host_version")):
        raise PluginPackageError(
            "incompatible_host_api",
            f"插件要求 host_api {manifest['host_api']}，当前宿主 {host_version}",
            path="host_api",
        )

    verified = VerifiedPackage(
        path=package,
        manifest=manifest,
        package_sha256=sha256_bytes(package.read_bytes()),
        key=key,
        signature=signature,
        files=tuple(sorted(manifest_files)),
    )
    if catalog is not None:
        _check_dependencies(verified, {item.plugin_id: item for item in catalog})
    return verified


def _check_dependencies(package: VerifiedPackage, catalog: dict[str, VerifiedPackage]) -> None:
    for index, dependency in enumerate(package.manifest.get("dependencies", [])):
        field = f"dependencies[{index}]"
        dependency_id = dependency["id"]
        target = catalog.get(dependency_id)
        if target is None:
            raise PluginPackageError("missing_dependency", f"缺少依赖插件: {dependency_id}", path=field)
        if SemVer.parse(str(target.manifest["version"])) != SemVer.parse(str(dependency["version"])):
            raise PluginPackageError(
                "dependency_mismatch",
                f"依赖 {dependency_id} 版本应为 {dependency['version']}，目录中为 {target.manifest['version']}",
                path=field,
            )
        digest = dependency.get("digest")
        if digest is not None and digest != target.package_sha256:
            raise PluginPackageError("dependency_mismatch", f"依赖 {dependency_id} 的包摘要不匹配", path=field)


def verify_catalog(
    directory: str | Path,
    trust: TrustStore,
    host_version: str,
) -> list[VerifiedPackage]:
    """Verify all ``.mko`` files in a directory and detect duplicate IDs/names."""

    directory = Path(directory)
    if not directory.is_dir():
        raise PluginPackageError("invalid_catalog", f"目录不存在: {directory}", path=str(directory))
    packages: list[VerifiedPackage] = []
    by_id: dict[str, VerifiedPackage] = {}
    by_name: dict[str, VerifiedPackage] = {}
    for path in sorted(directory.glob(f"*{PACKAGE_SUFFIX}")):
        package = verify_plugin(path, trust, host_version)
        if package.plugin_id in by_id:
            raise PluginPackageError(
                "duplicate_plugin_id",
                f"目录中重复插件 ID: {package.plugin_id}",
                path=str(path),
            )
        by_id[package.plugin_id] = package
        name = str(package.manifest.get("name", "")).strip()
        if name in by_name:
            raise PluginPackageError(
                "duplicate_plugin_name",
                f"目录中重复插件名称: {name}",
                path=str(path),
            )
        by_name[name] = package
        packages.append(package)
    for package in packages:
        _check_dependencies(package, by_id)
    return packages