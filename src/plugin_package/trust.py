"""Local trust roots and offline revocation lists for plugin packages."""

from __future__ import annotations

import base64
import binascii
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .errors import PluginPackageError

TRUST_SCHEMA = "mokoa.trust-root/v1"
REVOCATION_SCHEMA = "mokoa.revocations/v1"
_KEY_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{2,127}$")


@dataclass(frozen=True)
class TrustedKey:
    key_id: str
    algorithm: str
    public_key: Ed25519PublicKey
    publisher: str


def _read_json(path: Path, *, code: str) -> Any:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise PluginPackageError(code, f"无法读取 {path}: {exc}", path=str(path)) from exc
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PluginPackageError(code, f"{path} 不是有效 UTF-8 JSON: {exc}", path=str(path)) from exc


def _decoded_public_key(value: str, *, key_id: str) -> Ed25519PublicKey:
    try:
        raw = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise PluginPackageError("invalid_trust_root", f"签发者 {key_id} 的公钥不是有效 base64") from exc
    if len(raw) != 32:
        raise PluginPackageError("invalid_trust_root", f"签发者 {key_id} 的 Ed25519 公钥必须是 32 字节")
    try:
        return Ed25519PublicKey.from_public_bytes(raw)
    except ValueError as exc:
        raise PluginPackageError("invalid_trust_root", f"签发者 {key_id} 的公钥无效") from exc


class TrustStore:
    """A local, offline trust root plus revoked signer IDs.

    Loading never performs network access. The verifier only uses public keys;
    private signing keys belong to publishers and are not part of this object.
    """

    def __init__(self, keys: dict[str, TrustedKey], revoked: set[str]) -> None:
        self._keys = keys
        self._revoked = revoked

    @classmethod
    def load(cls, trust_path: str | Path, revocation_path: str | Path | None = None) -> "TrustStore":
        trust = _read_json(Path(trust_path), code="invalid_trust_root")
        if not isinstance(trust, dict) or trust.get("schema") != TRUST_SCHEMA:
            raise PluginPackageError("invalid_trust_root", f"信任根 schema 必须是 {TRUST_SCHEMA}")
        entries = trust.get("keys")
        if not isinstance(entries, list) or not entries:
            raise PluginPackageError("invalid_trust_root", "信任根必须包含非空 keys 数组")
        keys: dict[str, TrustedKey] = {}
        for index, entry in enumerate(entries):
            if not isinstance(entry, dict):
                raise PluginPackageError("invalid_trust_root", f"keys[{index}] 必须是对象")
            key_id = entry.get("key_id")
            if not isinstance(key_id, str) or not _KEY_ID.fullmatch(key_id):
                raise PluginPackageError("invalid_trust_root", f"keys[{index}].key_id 格式无效")
            if key_id in keys:
                raise PluginPackageError("invalid_trust_root", f"信任根重复 key_id: {key_id}", path=key_id)
            if entry.get("algorithm") != "ed25519":
                raise PluginPackageError(
                    "invalid_trust_root",
                    f"签发者 {key_id} 只支持 ed25519",
                    path=key_id,
                )
            public_key = entry.get("public_key")
            if not isinstance(public_key, str):
                raise PluginPackageError("invalid_trust_root", f"签发者 {key_id} 缺少 public_key", path=key_id)
            publisher = entry.get("publisher")
            if not isinstance(publisher, str) or not publisher.strip():
                raise PluginPackageError("invalid_trust_root", f"签发者 {key_id} 缺少 publisher", path=key_id)
            keys[key_id] = TrustedKey(
                key_id=key_id,
                algorithm="ed25519",
                public_key=_decoded_public_key(public_key, key_id=key_id),
                publisher=publisher,
            )

        revoked: set[str] = set()
        if revocation_path is not None:
            revocations = _read_json(Path(revocation_path), code="invalid_revocations")
            if not isinstance(revocations, dict) or revocations.get("schema") != REVOCATION_SCHEMA:
                raise PluginPackageError(
                    "invalid_revocations",
                    f"撤销清单 schema 必须是 {REVOCATION_SCHEMA}",
                )
            entries = revocations.get("revoked")
            if entries is None:
                entries = []
            if not isinstance(entries, list):
                raise PluginPackageError("invalid_revocations", "revoked 必须是数组")
            for index, entry in enumerate(entries):
                if not isinstance(entry, dict):
                    raise PluginPackageError("invalid_revocations", f"revoked[{index}] 必须是对象")
                key_id = entry.get("key_id")
                if not isinstance(key_id, str) or not _KEY_ID.fullmatch(key_id):
                    raise PluginPackageError("invalid_revocations", f"revoked[{index}].key_id 格式无效")
                if key_id in revoked:
                    raise PluginPackageError("invalid_revocations", f"撤销清单重复 key_id: {key_id}", path=key_id)
                revoked.add(key_id)
        return cls(keys, revoked)

    def key(self, key_id: str) -> TrustedKey:
        if key_id in self._revoked:
            raise PluginPackageError("revoked_signer", f"签发者 {key_id} 已被本地撤销", path="signature.key_id")
        key = self._keys.get(key_id)
        if key is None:
            raise PluginPackageError("unknown_signer", f"签发者 {key_id} 不在本地信任根", path="signature.key_id")
        return key

    def verify(self, key_id: str, signature: bytes, message: bytes) -> TrustedKey:
        key = self.key(key_id)
        try:
            key.public_key.verify(signature, message)
        except InvalidSignature as exc:
            raise PluginPackageError("invalid_signature", f"签发者 {key_id} 的签名不匹配", path="signature.signature") from exc
        return key

    @property
    def key_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._keys))

    @property
    def revoked_key_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._revoked))