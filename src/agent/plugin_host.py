"""受限插件宿主（TASK-024 / Issue #32）。

基于 TASK-023 的插件包契约（验签/摘要/清单）实现宿主侧：

- **安装/启用/停用/升级/回滚可审计**：`installed` 表记录版本与指针，
  `audit` 表记录全部操作（时间/操作/插件/版本/结果）；升级先验包后切换，
  失败保留旧版本；回滚切回上一版本。
- 桌面宿主保留包管理与审计，默认关闭插件代码执行，待宿主隔离验收后接入。
- 受信任的开发样例可显式选择 Python 工作进程。源码检查、受限命名空间
  与 Job Object 的 UI 和生命周期约束用于测试开发样例。
- **超时/崩溃只终止插件**：子进程超时 kill、非零退出/异常只影响本次调用。
- **停用后动作消失**：停用即从动作目录注销（`actions_available` 不再可见）。
- **保留旧工具名兼容**：插件动作不得与内置动作同名（register_action 拒绝
  覆盖）；内置动作（calculate 等）继续以旧名提供。
- 最小开发样例：`plugins/samples/echo`（TASK-023 交付）+ 本文档测试中
  动态打包的样例。
"""
from __future__ import annotations

import io as _io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
import uuid
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

from src.agent.actions import Action, register_action, unregister_action
from src.plugin_package import PluginPackageError, VerifiedPackage, verify_plugin

HOST_API_VERSION = "1.0.0"
DEFAULT_DB_PATH = "data/plugin_host.db"
DEFAULT_PLUGINS_DIR = "data/plugins"
DEFAULT_TIMEOUT = 10.0
MAX_OUTPUT_BYTES = 64 * 1024
EXECUTION_UNAVAILABLE = "当前宿主尚未通过插件运行隔离验收，插件执行已关闭。"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS installed (
    plugin_id TEXT PRIMARY KEY,
    version TEXT NOT NULL,
    previous_version TEXT,
    enabled INTEGER NOT NULL DEFAULT 1,
    dir TEXT NOT NULL,
    installed_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    operation TEXT NOT NULL,
    plugin_id TEXT NOT NULL,
    version TEXT,
    ok INTEGER NOT NULL,
    detail TEXT
);
"""


def _now() -> str:
    import datetime
    return datetime.datetime.now().isoformat(timespec="seconds")


class PluginHostError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Windows Job Object 的 UI 限制和关闭时终止用于受信任开发样例。
# ---------------------------------------------------------------------------

_JOB_LIMIT_KILL_ON_CLOSE = 0x00002000
_JOB_LIMIT_ACTIVE_PROCESS = 0x00000008
# winnt.h JOBOBJECT_UIRESTRICTIONSClass 正确映射（TASK-024 三轮整改）：
_JOB_UILIMIT_HANDLES = 0x00000001
_JOB_UILIMIT_READCLIPBOARD = 0x00000002
_JOB_UILIMIT_WRITECLIPBOARD = 0x00000004
_JOB_UILIMIT_SYSTEMPARAMETERS = 0x00000008
_JOB_UILIMIT_DISPLAYSETTINGS = 0x00000010
_JOB_UILIMIT_GLOBALATOMS = 0x00000020
_JOB_UILIMIT_DESKTOP = 0x00000040
_JOB_UILIMIT_EXITWINDOWS = 0x00000080


def query_job_limits(job) -> dict[str, int]:
    """内核回读 Job 限制（测试断言用）：返回 limit_flags/active_limit/ui。"""
    import ctypes
    from ctypes import wintypes as _wt

    class _IO_COUNTERS(ctypes.Structure):
        _fields_ = [(n, ctypes.c_ulonglong) for n in (
            "ReadOperationCount", "WriteOperationCount",
            "OtherOperationCount", "ReadTransferCount",
            "WriteTransferCount", "OtherTransferCount")]

    class _BASIC(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", ctypes.c_uint32),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", ctypes.c_uint32),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", ctypes.c_uint32),
            ("Reserved", ctypes.c_uint32),
        ]

    class _EXTENDED(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _BASIC),
            ("IoInfo", _IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.QueryInformationJobObject.restype = _wt.BOOL
    kernel32.QueryInformationJobObject.argtypes = [
        _wt.HANDLE, ctypes.c_int, ctypes.c_void_p, _wt.DWORD,
        ctypes.POINTER(_wt.DWORD)]
    info = _EXTENDED()
    got = _wt.DWORD(0)
    ok = kernel32.QueryInformationJobObject(
        job, 9, ctypes.byref(info), ctypes.sizeof(_EXTENDED), ctypes.byref(got))
    if not ok:
        return {}
    basic = info.BasicLimitInformation
    # UI 掩码在独立信息类 JobObjectBasicUIRestrictions(=4)，单独回读
    ui = _wt.DWORD(0)
    if not kernel32.QueryInformationJobObject(
            job, 4, ctypes.byref(ui), ctypes.sizeof(ui), ctypes.byref(got)):
        ui.value = 0
    return {"limit_flags": basic.LimitFlags,
            "active_limit": basic.ActiveProcessLimit,
            "ui": ui.value}


def kernel32_close(handle: int) -> None:
    """关闭 Windows 内核句柄（Job/进程；仅 win32）。"""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ctypes.WinDLL("kernel32").CloseHandle(handle)
    except Exception:  # noqa: BLE001
        pass


def _apply_windows_job_restrictions(process) -> tuple[str | None, int | None]:
    """把子进程绑入受限 Job Object。

    Returns:
        (note, job_handle)。note=None 表示成功（job_handle 由调用方持有，
        执行完成后应 CloseHandle——kill-on-close 保证超时路径也终止）；
        失败返回原因且 job_handle=None（句柄已关闭，无泄漏）。

    结构体按 winnt.h 真实布局定义（x64）：
      JOBOBJECT_BASIC_LIMIT_INFORMATION = 64 字节（LARGE_INTEGER×2、
      LimitFlags DWORD、SIZE_T×2、ActiveProcessLimit DWORD、Affinity
      ULONG_PTR、PriorityClass DWORD、SchedulingClass DWORD，含自然对齐）；
      JOBOBJECT_EXTENDED_LIMIT_INFORMATION = BASIC(64) + IO_COUNTERS(48)
      + SIZE_T×4(32) = 144 字节。UI 限制不在该布局里——走独立信息类
      JobObjectBasicUIRestrictions(=4) 设置/回读（内核回读断言在测试中
      验证 limit_flags 与 UI 掩码生效）。
    """
    if sys.platform != "win32":
        return "non-windows（Job Object 不适用；OS 级隔离待平台化交付）", None
    try:
        import ctypes
        from ctypes import wintypes as _wt

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.restype = _wt.HANDLE
        kernel32.CreateJobObjectW.argtypes = [_wt.LPCWSTR, _wt.LPVOID]
        kernel32.SetInformationJobObject.argtypes = [
            _wt.HANDLE, ctypes.c_int, ctypes.c_void_p, _wt.DWORD]
        kernel32.SetInformationJobObject.restype = _wt.BOOL
        kernel32.AssignProcessToJobObject.argtypes = [_wt.HANDLE, _wt.HANDLE]
        kernel32.AssignProcessToJobObject.restype = _wt.BOOL
        kernel32.QueryInformationJobObject.argtypes = [
            _wt.HANDLE, ctypes.c_int, ctypes.c_void_p, _wt.DWORD,
            ctypes.POINTER(_wt.DWORD)]
        kernel32.QueryInformationJobObject.restype = _wt.BOOL
        kernel32.CloseHandle.argtypes = [_wt.HANDLE]

        class _IO_COUNTERS(ctypes.Structure):
            _fields_ = [(n, ctypes.c_ulonglong) for n in (
                "ReadOperationCount", "WriteOperationCount",
                "OtherOperationCount", "ReadTransferCount",
                "WriteTransferCount", "OtherTransferCount")]

        class _BASIC_LIMIT_INFO(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", ctypes.c_uint32),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", ctypes.c_uint32),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", ctypes.c_uint32),
                ("SchedulingClass", ctypes.c_uint32),
            ]

        class _EXTENDED_LIMIT_INFO(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", _BASIC_LIMIT_INFO),
                ("IoInfo", _IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        JobObjectExtendedLimitInformation = 9
        JobObjectBasicUIRestrictions = 4

        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return f"CreateJobObject 失败（error={ctypes.get_last_error()}）", None

        limits = _EXTENDED_LIMIT_INFO()
        basic = limits.BasicLimitInformation
        # fork 防护由源码改写承担（插件代码 import 全被改写，无法触达
        # subprocess）；不再设 ActiveProcessLimit——其与 Assign 已运行
        # 进程的语义冲突会导致 runner 误终止（复审实测 rc=101）
        basic.LimitFlags = _JOB_LIMIT_KILL_ON_CLOSE
        if not kernel32.SetInformationJobObject(
                job, JobObjectExtendedLimitInformation,
                ctypes.byref(limits), ctypes.sizeof(_EXTENDED_LIMIT_INFO)):
            err = ctypes.get_last_error()
            kernel32.CloseHandle(job)
            return f"SetInformationJobObject 失败（error={err}）", None

        # UI 限制走独立信息类 JobObjectBasicUIRestrictions(=4)：UIR…
        # 字段不在 BASIC_LIMIT_INFORMATION 布局里，赋值到未定义字段只是
        # 普通 Python 属性、不进内核（复审根因）。掩码取 CLI 子进程安全
        # 集合：剪贴板/退出系统/系统参数/显示设置/全局原子禁止；
        # DESKTOP/HANDLES 不设（CLI 子进程需要继承 stdout 管道句柄并访问
        # 非交互桌面），管道读写仍受管道闭包约束
        ui = _wt.DWORD(
            _JOB_UILIMIT_READCLIPBOARD | _JOB_UILIMIT_WRITECLIPBOARD
            | _JOB_UILIMIT_GLOBALATOMS | _JOB_UILIMIT_EXITWINDOWS
            | _JOB_UILIMIT_SYSTEMPARAMETERS | _JOB_UILIMIT_DISPLAYSETTINGS)
        if not kernel32.SetInformationJobObject(
                job, JobObjectBasicUIRestrictions,
                ctypes.byref(ui), ctypes.sizeof(ui)):
            err = ctypes.get_last_error()
            kernel32.CloseHandle(job)
            return f"SetInformationJobObject(UI) 失败（error={err}）", None

        handle = int(process._handle)  # subprocess 伪句柄
        if not kernel32.AssignProcessToJobObject(job, handle):
            err = ctypes.get_last_error()
            kernel32.CloseHandle(job)
            return f"AssignProcessToJobObject 失败（error={err}）", None
        return None, int(job)  # 成功：返回 job 句柄供调用方回读/关闭
    except Exception as exc:  # noqa: BLE001 —— OS 限制失败不阻断，如实降级
        return f"OS 限制应用异常：{type(exc).__name__}", None


class PluginHost:
    """插件安装、生命周期与受限执行；线程安全。"""

    def __init__(self, *, db_path: str | Path | None = None,
                 plugins_dir: str | Path | None = None,
                 trust_store=None, project_root: str | None = None,
                 runner_timeout: float = DEFAULT_TIMEOUT,
                 allow_trusted_python: bool = False) -> None:
        root = Path(project_root) if project_root else Path.cwd()
        self.plugins_dir = Path(plugins_dir) if plugins_dir else root / DEFAULT_PLUGINS_DIR
        self.plugins_dir.mkdir(parents=True, exist_ok=True)
        resolved_db = Path(db_path) if db_path else (
            root / DEFAULT_DB_PATH if project_root else
            Path(os.getenv("OS_AGENT_PLUGIN_DB") or DEFAULT_DB_PATH))
        resolved_db.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(resolved_db), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        self.trust_store = trust_store
        self.runner_timeout = runner_timeout
        self._allow_trusted_python = allow_trusted_python
        self._registered: dict[str, list[str]] = {}  # plugin_id -> 已注册动作名
        if allow_trusted_python:
            self._restore_actions()

    def close(self) -> None:
        with self._lock:
            for plugin_id in list(self._registered):
                self._unregister_actions(plugin_id)
            self._conn.close()

    def _restore_actions(self) -> None:
        for plugin in self.list_installed():
            if not plugin["enabled"]:
                continue
            plugin_id, version = plugin["plugin_id"], plugin["version"]
            try:
                manifest = self._read_installed_manifest(plugin)
                self._register_actions_from_manifest(plugin_id, manifest)
            except (OSError, ValueError, PluginHostError) as exc:
                self._audit("restore_actions", plugin_id, version=version,
                            ok=False, detail=str(exc))

    def _read_installed_manifest(self, plugin) -> dict[str, Any]:
        target = self.plugins_dir / plugin["plugin_id"] / plugin["version"]
        if (not target.resolve().is_relative_to(self.plugins_dir.resolve())
                or Path(plugin["dir"]).resolve() != target.resolve()):
            raise PluginHostError("已安装插件目录无效。")
        manifest = json.loads((target / "manifest.json").read_text(encoding="utf-8"))
        if (not isinstance(manifest, dict) or manifest.get("id") != plugin["plugin_id"]
                or manifest.get("version") != plugin["version"]
                or not isinstance(manifest.get("actions"), list)
                or not all(isinstance(action, dict) for action in manifest["actions"])):
            raise PluginHostError("已安装插件清单无效。")
        return manifest

    # -- 审计 ---------------------------------------------------------------

    def _audit(self, operation: str, plugin_id: str, *, version: str | None,
               ok: bool, detail: str | None = None) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO audit(at, operation, plugin_id, version, ok, detail)"
                " VALUES(:at, :op, :pid, :v, :ok, :d)",
                {"at": _now(), "op": operation, "pid": plugin_id, "v": version,
                 "ok": 1 if ok else 0, "d": detail})

    def audit_log(self, *, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM audit ORDER BY id DESC LIMIT :l",
                {"l": limit}).fetchall()
        return [dict(r) for r in rows]

    # -- 安装/升级/回滚 ------------------------------------------------------

    def _snapshot(self, package_path: str | Path) -> tuple[bytes, Path]:
        """读取 .mko 全部字节为**一次性快照**，写入临时文件。

        验签与解包都必须使用这份快照（复核项：实测仍各读两次源包）——
        调用方从本方法返回的临时文件读取，源包自此不再被触碰。
        """
        data = Path(package_path).read_bytes()
        tmp = self.plugins_dir / f".snapshot-{uuid.uuid4().hex}.mko"
        tmp.write_bytes(data)
        return data, tmp

    def _extract_payload_file(self, package_file: Path, target: Path) -> None:
        """从已验证的包文件解包 payload；防路径穿越（zip slip）。"""
        target.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(package_file) as zf:
            for member in zf.namelist():
                if member in ("manifest.json", "signature.json"):
                    continue
                pure = PurePosixPath(member)
                if pure.is_absolute() or ".." in pure.parts or "\\" in member:
                    raise PluginHostError(f"包内路径非法：{member}")
                destination = (target / pure).resolve()
                if not destination.is_relative_to(target.resolve()):
                    raise PluginHostError(f"包内路径越界：{member}")
            zf.extractall(target)

    def _scan_payload_forbidden_apis(self, target: Path) -> list[str]:
        """宿主侧强制（TASK-024 复核项）：AST 静态扫描 payload。

        禁止联网（socket / urllib.request / httpx / requests）、进程启动
        （subprocess / os.system / os.popen / os.exec*）、绕过面（ctypes、
        builtins 赋值、os.open）。命中即拒绝安装/升级——由宿主强制执行，
        不依赖子进程内的运行时限制。
        """
        violations: list[str] = []
        import ast
        for py in sorted(target.rglob("*.py")):
            try:
                tree = ast.parse(py.read_text(encoding="utf-8", errors="replace"))
            except (OSError, SyntaxError) as exc:
                violations.append(f"{py.name}: 解析失败（{exc}）")
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        root = alias.name.split(".")[0]
                        if root in {"socket", "subprocess", "ctypes", "httpx",
                                    "requests", "urllib3"} or alias.name in {
                                "urllib.request", "os.open"}:
                            violations.append(f"{py.name}:{node.lineno} import {alias.name}")
                elif isinstance(node, ast.ImportFrom):
                    root = (node.module or "").split(".")[0]
                    if root in {"socket", "subprocess", "ctypes", "httpx",
                                "requests", "urllib3", "urllib"}:
                        violations.append(
                            f"{py.name}:{node.lineno} from {node.module} import")
                elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                    func = node.func
                    if isinstance(func.value, ast.Name) and func.value.id == "os" \
                            and func.attr in {"system", "popen", "exec", "execv",
                                              "execve", "spawn", "open"}:
                        violations.append(
                            f"{py.name}:{node.lineno} os.{func.attr}()")
                elif isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name) \
                        and node.targets[0].id == "open":
                    # 恢复/替换 builtins.open 的补丁被拒绝
                    violations.append(f"{py.name}:{node.lineno} 赋值 open（绕过宿主限制）")
        return violations


    def install(self, package_path: str | Path) -> dict[str, Any]:
        """安装（验包 → 解包 → 静态扫描 → 注册动作）；同版本重复安装拒绝。

        验签与解包使用**同一份字节快照**（一次性临时文件）。
        """
        snapshot_tmp: Path | None = None
        try:
            try:
                _, snapshot_tmp = self._snapshot(package_path)
                package = verify_plugin(snapshot_tmp, self.trust_store, HOST_API_VERSION)
            except PluginPackageError as exc:
                self._audit("install", str(package_path), version=None, ok=False,
                            detail=str(exc))
                raise PluginHostError(f"验包失败：{exc}") from exc
            plugin_id = package.plugin_id
            version = str(package.manifest["version"])
            with self._lock:
                row = self._conn.execute(
                    "SELECT * FROM installed WHERE plugin_id=:p",
                    {"p": plugin_id}).fetchone()
            if row is not None:
                if row["version"] == version:
                    self._audit("install", plugin_id, version=version, ok=False,
                                detail="同版本已安装")
                    raise PluginHostError("同版本已安装；升级请使用 upgrade。")
                return self.upgrade(package_path)
            target = self.plugins_dir / plugin_id / version
            try:
                # 与验签**同一份快照**解包（复核项）+ AST 扫描
                self._extract_payload_file(snapshot_tmp, target)
                violations = self._scan_payload_forbidden_apis(target)
                if violations:
                    raise PluginHostError(
                        "宿主侧强制：payload 含禁用 API——"
                        + "；".join(violations[:5]))
            except (OSError, zipfile.BadZipFile, PluginHostError) as exc:
                self._audit("install", plugin_id, version=version, ok=False,
                            detail=str(exc))
                shutil.rmtree(target, ignore_errors=True)
                raise
            with self._lock, self._conn:
                self._conn.execute(
                    "INSERT INTO installed(plugin_id, version, previous_version,"
                    " enabled, dir, installed_at, updated_at)"
                    " VALUES(:p, :v, NULL, 1, :d, :ts, :ts)",
                    {"p": plugin_id, "v": version, "d": str(target), "ts": _now()})
            self._register_actions_from_manifest(plugin_id, package.manifest)
            self._audit("install", plugin_id, version=version, ok=True)
            return {"plugin_id": plugin_id, "version": version, "enabled": True}
        finally:
            if snapshot_tmp is not None:
                try:
                    snapshot_tmp.unlink()
                except OSError:
                    pass

    def upgrade(self, package_path: str | Path) -> dict[str, Any]:
        """升级：验包/解包/扫描使用**同一份字节快照**；失败保留旧版本。"""
        snapshot_tmp: Path | None = None
        try:
            try:
                _, snapshot_tmp = self._snapshot(package_path)
                package = verify_plugin(snapshot_tmp, self.trust_store, HOST_API_VERSION)
            except PluginPackageError as exc:
                self._audit("upgrade", str(package_path), version=None, ok=False,
                            detail=f"验包失败：{exc}")
                raise PluginHostError(f"验包失败：{exc}") from exc
            plugin_id = package.plugin_id
            version = str(package.manifest["version"])
            with self._lock:
                row = self._conn.execute(
                    "SELECT * FROM installed WHERE plugin_id=:p",
                    {"p": plugin_id}).fetchone()
            if row is None:
                raise PluginHostError("插件未安装；请先 install。")
            old_version = row["version"]
            if version == old_version:
                raise PluginHostError("同版本无需升级。")
            target = self.plugins_dir / plugin_id / version
            try:
                # 与验签**同一份快照**解包（复核项）+ AST 扫描
                self._extract_payload_file(snapshot_tmp, target)
                violations = self._scan_payload_forbidden_apis(target)
                if violations:
                    raise PluginHostError(
                        "宿主侧强制：payload 含禁用 API——"
                        + "；".join(violations[:5]))
            except (OSError, zipfile.BadZipFile, PluginHostError) as exc:
                self._audit("upgrade", plugin_id, version=version, ok=False,
                            detail=f"解包/扫描失败，保留 {old_version}：{exc}")
                shutil.rmtree(target, ignore_errors=True)
                raise
            with self._lock, self._conn:
                self._conn.execute(
                    "UPDATE installed SET version=:v, previous_version=:pv,"
                    " dir=:d, updated_at=:ts WHERE plugin_id=:p",
                    {"v": version, "pv": old_version, "d": str(target),
                     "ts": _now(), "p": plugin_id})
            self._unregister_actions(plugin_id)
            if row["enabled"]:
                self._register_actions_from_manifest(plugin_id, package.manifest)
            self._audit("upgrade", plugin_id, version=version, ok=True,
                        detail=f"{old_version} → {version}")
            return {"plugin_id": plugin_id, "version": version,
                    "previous_version": old_version}
        finally:
            if snapshot_tmp is not None:
                try:
                    snapshot_tmp.unlink()
                except OSError:
                    pass

    def rollback(self, plugin_id: str) -> dict[str, Any]:
        """回滚到上一版本（目录仍在才可回滚）。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM installed WHERE plugin_id=:p",
                {"p": plugin_id}).fetchone()
        if row is None:
            raise PluginHostError("插件未安装。")
        previous = row["previous_version"]
        if not previous:
            raise PluginHostError("无可回滚的版本。")
        target = self.plugins_dir / plugin_id / previous
        if not target.exists():
            raise PluginHostError(f"上一版本目录缺失：{previous}")
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE installed SET version=:v, previous_version=:pv,"
                " dir=:d, updated_at=:ts WHERE plugin_id=:p",
                {"v": previous, "pv": row["version"], "d": str(target),
                 "ts": _now(), "p": plugin_id})
        self._unregister_actions(plugin_id)
        if row["enabled"] and self._allow_trusted_python:
            manifest = self._read_installed_manifest(
                {"plugin_id": plugin_id, "version": previous, "dir": str(target)})
            self._register_actions_from_manifest(plugin_id, manifest)
        self._audit("rollback", plugin_id, version=previous, ok=True,
                    detail=f"{row['version']} → {previous}")
        return {"plugin_id": plugin_id, "version": previous}

    # -- 启用/停用 -----------------------------------------------------------

    def set_enabled(self, plugin_id: str, enabled: bool) -> dict[str, Any]:
        """停用后动作消失（从统一动作目录注销）；启用恢复。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM installed WHERE plugin_id=:p",
                {"p": plugin_id}).fetchone()
        if row is None:
            raise PluginHostError("插件未安装。")
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE installed SET enabled=:e, updated_at=:ts WHERE plugin_id=:p",
                {"e": 1 if enabled else 0, "ts": _now(), "p": plugin_id})
        if enabled and self._allow_trusted_python:
            manifest = self._read_installed_manifest(row)
            self._register_actions_from_manifest(plugin_id, manifest)
        else:
            self._unregister_actions(plugin_id)
        self._audit("enable" if enabled else "disable", plugin_id,
                    version=row["version"], ok=True)
        return {"plugin_id": plugin_id, "enabled": enabled}

    def list_installed(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM installed ORDER BY updated_at DESC").fetchall()
        return [{**dict(r), "enabled": bool(r["enabled"]),
                 "executable": r["plugin_id"] in self._registered} for r in rows]

    # -- 动作注册（统一目录） -------------------------------------------------

    def _register_actions_from_manifest(self, plugin_id: str,
                                        manifest: dict[str, Any]) -> None:
        if not self._allow_trusted_python:
            return
        actions = manifest.get("actions") or []
        names: list[str] = []
        for spec in actions:
            action_id = str(spec.get("id"))
            name = f"plugin.{plugin_id}.{action_id}"
            schema = {
                "name": name,
                "description": f"[插件 {plugin_id}] {spec.get('name') or action_id}",
                "parameters": spec.get("parameters") or {"type": "object"},
            }
            action = Action(
                name=name, schema=schema,
                validate=lambda args: dict(args),
                execute=self._make_executor(plugin_id, action_id),
                mutates=bool(spec.get("mutates", False)),
            )
            register_action(action)
            names.append(name)
        self._registered[plugin_id] = names

    def _register_actions(self, plugin_id: str, package: VerifiedPackage) -> None:
        self._register_actions_from_manifest(plugin_id, package.manifest)

    def _unregister_actions(self, plugin_id: str) -> None:
        for name in self._registered.pop(plugin_id, []):
            unregister_action(name)

    def _make_executor(self, plugin_id: str, action_id: str):
        def execute(**kwargs) -> tuple[bool, str]:
            return self.run_action(plugin_id, action_id, kwargs)
        return execute

    # -- 受限执行（子进程隔离） -----------------------------------------------

    def _build_restricted_source(self, plugin_dir: Path, action_id: str) -> str:
        """**执行时宿主强制**（TASK-024 复核项）：从 payload 提取动作代码，
        **剥离全部 import 语句**后生成受限模块源码。

        宿主决定插件代码以什么形态执行：生成的代码不含任何 import——
        插件试图联网/启动进程/绕过文件限制时在 NameError 处失败，而不是
        依赖子进程内运行时限制。同模块的其它函数定义与常量一并保留
        （目标动作可调用它们，它们的 import 同样被剥离）。
        """
        import ast as _ast
        entry = str(json.loads(
            (plugin_dir / "manifest.json").read_text(encoding="utf-8")).get("entry") or "")
        entry_path = (plugin_dir / entry).resolve()
        if not entry_path.is_relative_to(plugin_dir.resolve()) or not entry_path.is_file():
            raise PluginHostError(f"入口文件不存在或越界：{entry}")
        tree = _ast.parse(entry_path.read_text(encoding="utf-8", errors="replace"))
        # 保留全部模块级定义（import 行在下一步文本后处理中改写）
        if not tree.body:
            raise PluginHostError(f"插件未实现可提取的动作：{action_id}")
        code = _ast.unparse(_ast.Module(body=tree.body, type_ignores=[]))
        # 第二步（文本后处理，unparse 格式可控）：import 行改写——
        # 受控标准库（time，无网络/进程面）改写为从宿主受限模块表取值；
        # 其余 import 行改写为直接 PermissionError（宿主强制）。
        _SAFE = {"time"}
        lines = []
        for line in code.splitlines():
            stripped = line.lstrip()
            indent = line[:len(line) - len(stripped)]
            if stripped.startswith("import "):
                name = stripped.split()[1].split(".")[0]
                if name in _SAFE:
                    # 受控标准库：从宿主受限模块表取值（保留原缩进）
                    lines.append(f"{indent}{name} = __restricted_modules__['{name}']")
                else:
                    lines.append(
                        f"{indent}raise PermissionError("
                        f"'插件 import {name} 被宿主拒绝（未授权能力）。')")
            elif stripped.startswith("from ") and " import " in stripped:
                module_name = stripped.split()[1].split(".")[0]
                bound = stripped.rsplit(" import ", 1)[1].strip().split(",")[0]
                if module_name in _SAFE:
                    lines.append(
                        f"{indent}{bound} = __restricted_modules__['{module_name}'].{bound}")
                else:
                    lines.append(
                        f"{indent}raise PermissionError("
                        f"'插件 import {module_name} 被宿主拒绝（未授权能力）。')")
            else:
                lines.append(line)
        return chr(10).join(lines)

    def _build_restricted_source_from_source(self, source_dir: Path,
                                             action_id: str) -> str:
        """源目录版受限代码生成（测试断言用，无需安装）。"""
        import ast as _ast
        entry_path = (source_dir / "payload" / "main.py").resolve()
        if not entry_path.is_file():
            raise PluginHostError(f"入口文件不存在：{entry_path}")
        tree = _ast.parse(entry_path.read_text(encoding="utf-8", errors="replace"))
        _SAFE_MODULES = {"time"}
        for node in _ast.walk(tree):
            for field in ("body", "orelse", "finalbody"):
                block = getattr(node, field, None)
                if not isinstance(block, list):
                    continue
                new_block = []
                for stmt in block:
                    if isinstance(stmt, _ast.Import):
                        name = stmt.names[0].name
                        if name in _SAFE_MODULES:
                            new_block.append(_ast.Assign(
                                targets=[_ast.Name(id=name.split(".")[0],
                                                   ctx=_ast.Store())],
                                value=_ast.Subscript(
                                    value=_ast.Name(id="__restricted_modules__",
                                                    ctx=_ast.Load()),
                                    slice=_ast.Constant(value=name),
                                    ctx=_ast.Load())))
                        else:
                            new_block.append(_ast.Raise(
                                exc=_ast.Call(
                                    func=_ast.Name(id="PermissionError",
                                                   ctx=_ast.Load()),
                                    args=[_ast.Constant(
                                        value=f"插件 import {name} 被宿主拒绝"
                                              "（未授权能力）。")],
                                    keywords=[]),
                                cause=None))
                        for extra in stmt.names[1:]:
                            if extra.name in _SAFE_MODULES:
                                new_block.append(_ast.Assign(
                                    targets=[_ast.Name(
                                        id=extra.asname or extra.name.split(".")[0],
                                        ctx=_ast.Store())],
                                    value=_ast.Subscript(
                                        value=_ast.Name(id="__restricted_modules__",
                                                        ctx=_ast.Load()),
                                        slice=_ast.Constant(value=extra.name),
                                        ctx=_ast.Load())))
                    elif isinstance(stmt, _ast.ImportFrom):
                        root = (stmt.module or "").split(".")[0]
                        if root in _SAFE_MODULES:
                            for alias in stmt.names:
                                bound = alias.asname or alias.name
                                new_block.append(_ast.Assign(
                                    targets=[_ast.Name(id=bound, ctx=_ast.Store())],
                                    value=_ast.Attribute(
                                        value=_ast.Subscript(
                                            value=_ast.Name(
                                                id="__restricted_modules__",
                                                ctx=_ast.Load()),
                                            slice=_ast.Constant(value=root),
                                            ctx=_ast.Load()),
                                        attr=alias.name, ctx=_ast.Load())))
                        else:
                            new_block.append(_ast.Raise(
                                exc=_ast.Call(
                                    func=_ast.Name(id="PermissionError",
                                                   ctx=_ast.Load()),
                                    args=[_ast.Constant(
                                        value=f"插件 import {stmt.module} 被宿主拒绝"
                                              "（未授权能力）。")],
                                    keywords=[]),
                                cause=None))
                    else:
                        new_block.append(stmt)
                setattr(node, field, new_block)
        return _ast.unparse(_ast.Module(body=tree.body, type_ignores=[]))

    def run_action(self, plugin_id: str, action_id: str, args: dict[str, Any], *,
                   timeout: float | None = None) -> tuple[bool, str]:
        """在独立子进程中执行插件动作；超时/崩溃只终止插件。

        执行代码由宿主**生成**（_build_restricted_source：剥离 import），
        子进程只执行受限代码——执行时的文件/网络/进程约束由宿主强制。
        """
        if not self._allow_trusted_python:
            return False, EXECUTION_UNAVAILABLE
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM installed WHERE plugin_id=:p",
                {"p": plugin_id}).fetchone()
        if row is None or not row["enabled"]:
            return False, "插件未安装或已停用。"
        plugin_dir = Path(row["dir"])
        try:
            code = self._build_restricted_source(plugin_dir, action_id)
        except (PluginHostError, OSError) as exc:
            return False, str(exc)
        payload = {
            "code": code,
            "action": action_id,
            "args": args,
            "plugin_dir": str(plugin_dir.resolve()),
        }
        runner = Path(__file__).resolve().parent / "plugin_runner.py"
        try:
            # Windows 先绑定 Job，再把样例代码发送给工作进程。
            process = subprocess.Popen(
                [sys.executable, "-I", str(runner)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=str(plugin_dir),
            )
        except OSError as exc:
            return False, f"插件进程启动失败：{exc}"
        restrict_note, job_handle = _apply_windows_job_restrictions(process)
        # OS 限制降级/生效均落审计（复核项：降级不得仅 pass）
        stderr_tail = ""
        try:
            self._audit("job_restrict", plugin_id, version=row["version"],
                        ok=restrict_note is None, detail=restrict_note)
            if sys.platform == "win32" and not job_handle:
                return False, f"插件工作进程保护未生效：{restrict_note or '无法绑定 Windows Job'}"
            raw, _err = process.communicate(
                input=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                timeout=timeout or self.runner_timeout)
        except subprocess.TimeoutExpired:
            # 超时只终止插件子进程；宿主继续
            return False, f"插件执行超时（{timeout or self.runner_timeout:.0f} 秒），已终止。"
        except OSError as exc:
            return False, f"插件通信失败：{exc}"
        finally:
            if job_handle:
                kernel32_close(job_handle)
            if process.poll() is None:
                process.kill()
            process.communicate()
        stderr_tail = (_err or b"")[-400:].decode("utf-8", errors="replace")
        raw = raw[:MAX_OUTPUT_BYTES]
        try:
            result = json.loads(raw.decode("utf-8", errors="replace") or "{}")
        except ValueError:
            return False, (f"插件返回无效结果（退出码 {process.returncode}）。"
                           f"stderr：{stderr_tail}")
        if not result.get("ok"):
            error = str(result.get("error") or "插件执行失败。")
            if stderr_tail:
                error += f"（stderr：{stderr_tail}）"
            return False, error
        return True, str(result.get("result", ""))
