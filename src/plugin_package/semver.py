"""Small SemVer and range implementation for the plugin contract.

Only syntax needed by the manifest is supported: versions are SemVer 2.0
``MAJOR.MINOR.PATCH[-PRERELEASE][+BUILD]``; host API ranges are whitespace or
comma separated comparator clauses such as ``>=1.0.0 <2.0.0``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import total_ordering

from .errors import PluginPackageError

_SEMVER = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)


@total_ordering
@dataclass(frozen=True)
class SemVer:
    major: int
    minor: int
    patch: int
    prerelease: tuple[str, ...] = ()
    build: tuple[str, ...] = ()

    @classmethod
    def parse(cls, value: str, *, field: str = "version") -> "SemVer":
        if not isinstance(value, str):
            raise PluginPackageError("invalid_version", "版本必须是字符串", path=field)
        match = _SEMVER.fullmatch(value.strip())
        if not match:
            raise PluginPackageError(
                "invalid_version",
                f"版本 {value!r} 不是 SemVer（示例：1.2.3）",
                path=field,
            )
        prerelease = tuple(match.group(4).split(".")) if match.group(4) else ()
        build = tuple(match.group(5).split(".")) if match.group(5) else ()
        return cls(int(match.group(1)), int(match.group(2)), int(match.group(3)), prerelease, build)

    def _compare(self, other: "SemVer") -> int:
        left = (self.major, self.minor, self.patch)
        right = (other.major, other.minor, other.patch)
        if left != right:
            return -1 if left < right else 1
        if self.prerelease == other.prerelease:
            return 0
        if not self.prerelease:
            return 1
        if not other.prerelease:
            return -1
        for a, b in zip(self.prerelease, other.prerelease):
            if a == b:
                continue
            a_num, b_num = a.isdigit(), b.isdigit()
            if a_num and b_num:
                return -1 if int(a) < int(b) else 1
            if a_num != b_num:
                return -1 if a_num else 1
            return -1 if a < b else 1
        return -1 if len(self.prerelease) < len(other.prerelease) else 1

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, SemVer):
            return NotImplemented
        return self._compare(other) < 0

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, SemVer):
            return False
        return self._compare(other) == 0

    def __hash__(self) -> int:
        return hash((self.major, self.minor, self.patch, self.prerelease))

    def __str__(self) -> str:
        base = f"{self.major}.{self.minor}.{self.patch}"
        if self.prerelease:
            base += "-" + ".".join(self.prerelease)
        return base


@dataclass(frozen=True)
class Comparator:
    operator: str
    version: SemVer

    def matches(self, version: SemVer) -> bool:
        result = version._compare(self.version)
        if self.operator == ">=":
            return result >= 0
        if self.operator == ">":
            return result > 0
        if self.operator == "<=":
            return result <= 0
        if self.operator == "<":
            return result < 0
        if self.operator == "=":
            return result == 0
        raise AssertionError(self.operator)


class VersionRange:
    """A conjunction of SemVer comparator clauses."""

    def __init__(self, comparators: list[Comparator], raw: str) -> None:
        self.comparators = tuple(comparators)
        self.raw = raw

    @classmethod
    def parse(cls, value: str, *, field: str = "host_api") -> "VersionRange":
        if not isinstance(value, str) or not value.strip():
            raise PluginPackageError("invalid_host_api", "host_api 必须是非空范围字符串", path=field)
        clauses = [part for part in re.split(r"[\s,]+", value.strip()) if part]
        if not clauses:
            raise PluginPackageError("invalid_host_api", "host_api 至少需要一个比较条件", path=field)
        comparators: list[Comparator] = []
        for clause in clauses:
            match = re.fullmatch(r"(>=|<=|>|<|=)?\s*(.+)", clause)
            if not match:
                raise PluginPackageError("invalid_host_api", f"无法解析 host_api 子句 {clause!r}", path=field)
            operator = match.group(1) or "="
            version = SemVer.parse(match.group(2), field=field)
            comparators.append(Comparator(operator, version))
        return cls(comparators, value)

    def contains(self, version: SemVer) -> bool:
        return all(comparator.matches(version) for comparator in self.comparators)

    def __contains__(self, version: object) -> bool:
        if isinstance(version, str):
            version = SemVer.parse(version)
        if not isinstance(version, SemVer):
            return False
        return self.contains(version)

    def __str__(self) -> str:
        return " ".join(f"{item.operator}{item.version}" for item in self.comparators)