"""受信任插件开发样例的 Python 工作进程（TASK-024 / Issue #32）。

由 `PluginHost.run_action` 以 `python -I`（隔离模式）在独立子进程中启动；
stdin 收 JSON 载荷，stdout 回 JSON 结果。**在导入插件代码之前**收紧：

- `builtins.open`：仅允许插件目录内的路径（越界读取被拒绝）；
- `socket`：禁用网络连接（联网被阻止）；
- `subprocess` / `os.system` / `os.popen`：禁用进程启动（未授权进程被阻止）。

限制在子进程内生效，插件崩溃/超时不影响宿主。本脚本不导入项目其它模块
（`-I` 隔离模式下 sys.path 不含项目目录），保持自包含。
"""
from __future__ import annotations

import builtins
import json
import os
import sys
from pathlib import Path


def _install_restrictions(plugin_dir: Path) -> None:
    root = plugin_dir.resolve()
    real_open = builtins.open

    def guarded_open(file, mode="r", *args, **kwargs):
        try:
            target = Path(file).resolve()
        except (TypeError, ValueError, OSError):
            raise PermissionError(f"插件仅可访问自身目录内的文件：{file!r}")
        if not target.is_relative_to(root):
            raise PermissionError(f"越界读取被拒绝：{file}")
        if mode not in {"r", "rt", "rb"} or len(args) > 5 or "opener" in kwargs:
            raise PermissionError("插件文件接口仅支持只读。")
        return real_open(target, mode, *args, **kwargs)

    builtins.open = guarded_open

    # 联网：禁用 socket 连接
    import socket as _socket

    def _blocked_connect(*_a, **_k):
        raise PermissionError("插件联网被拒绝（宿主未授予网络能力）。")

    _socket.socket.connect = _blocked_connect  # type: ignore[method-assign]
    _socket.create_connection = _blocked_connect  # type: ignore[assignment]

    # 进程启动：禁用 subprocess / os.system / os.popen
    import subprocess as _subprocess

    def _blocked_spawn(*_a, **_k):
        raise PermissionError("插件启动进程被拒绝（宿主未授予进程能力）。")

    _subprocess.Popen = _blocked_spawn  # type: ignore[assignment]
    _subprocess.run = _blocked_spawn  # type: ignore[assignment]
    os.system = _blocked_spawn  # type: ignore[assignment]
    os.popen = _blocked_spawn  # type: ignore[assignment]


# 受限命名空间：宿主已剥离全部 import——执行环境只提供白名单内建函数
# 与受控文件访问（仅插件目录内）。import 在受限命名空间中不可用
# （__import__ 不提供），插件试图联网/启动进程/绕过文件限制时直接失败。
_SAFE_BUILTINS = {
    "len": len, "range": range, "str": str, "int": int, "float": float,
    "bool": bool, "list": list, "dict": dict, "set": set, "tuple": tuple,
    "abs": abs, "min": min, "max": max, "sum": sum, "sorted": sorted,
    "enumerate": enumerate, "zip": zip, "map": map, "filter": filter,
    "round": round, "repr": repr, "isinstance": isinstance,
    "hasattr": hasattr, "getattr": getattr, "any": any, "all": all,
    "ValueError": ValueError, "TypeError": TypeError, "KeyError": KeyError,
    "RuntimeError": RuntimeError,
}


def main() -> int:
    payload = json.loads(sys.stdin.buffer.read().decode("utf-8") or "{}")
    code = str(payload.get("code") or "")
    action_id = str(payload.get("action") or "")
    args = payload.get("args") or {}
    plugin_dir = Path(payload.get("plugin_dir") or ".")

    def emit(ok: bool, *, result: str = "", error: str = "") -> int:
        # 显式 UTF-8 写入 stdout（Windows 控制台默认 GBK 会导致中文乱码）
        payload_bytes = json.dumps({"ok": ok, "result": result, "error": error},
                                   ensure_ascii=False).encode("utf-8")
        sys.stdout.buffer.write(payload_bytes)
        sys.stdout.buffer.flush()
        return 0

    if not code:
        return emit(False, error="宿主未提供受限代码。")

    _install_restrictions(plugin_dir)

    safe_builtins = dict(_SAFE_BUILTINS)
    safe_builtins["open"] = builtins.open

    try:
        import time as _time
        namespace = {"__builtins__": safe_builtins, "__name__": "mokoa_restricted",
                     # 宿主提供的受限模块表（仅无网络/进程面的标准库）
                     "__restricted_modules__": {"time": _time}}
        # 宿主生成的受限代码（import 已剥离）——执行环境无 __import__，
        # 任何联网/进程/越界尝试在 NameError/PermissionError 处失败
        exec(compile(code, "<plugin-restricted>", "exec"), namespace)
        handler = namespace.get(action_id)
        if handler is None or not callable(handler):
            return emit(False, error=f"受限代码未实现动作：{action_id}")
        value = handler(**args)
        return emit(True, result=str(value))
    except PermissionError as exc:
        return emit(False, error=str(exc))
    except Exception as exc:  # noqa: BLE001 —— 插件异常只影响本次调用
        return emit(False, error=f"插件执行异常：{type(exc).__name__}: {exc}")


if __name__ == "__main__":
    raise SystemExit(main())
