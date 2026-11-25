"""统一 Agent 动作目录与调用入口（TASK-021 / Issue #29)。

此前工具的实现与分发分散在 service.py 三处：工具 schema 常量、`_run_local_tool`
的 if/elif 校验、`_tools` 节点里的第二层 if/elif 执行。模型调用与 UI/API 没有
共同入口，同一动作无法保证等价结果。

本模块提供唯一的动作目录（`ACTIONS`）与唯一分发入口（`dispatch_action`）：
- 每个动作一个 `Action` 记录：模型 schema、参数校验、执行函数、能力开关、
  是否变更文件（mutating）与是否验证工件（artifact）。
- `tools_for_model()` 按能力开关生成模型工具列表；模型返回的 tool_call 与
  UI/API 的直接调用都走同一个 `dispatch_action`，保证等价结果与错误码。
- 未知动作返回结构化 `tool_unavailable`，不抛异常，模型可继续生成回答。

memory 检索依赖编排器与请求上下文，exec/web 依赖运行时钟，因此通过
`ActionContext` 注入，动作本身保持纯函数。
"""
from __future__ import annotations

import ast
import fnmatch
import json
import os
import pathlib
import re
import shlex
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from time import perf_counter
from typing import Any, Callable
from urllib.parse import urlparse

from src.agent.workspace import WorkspaceError, get_workspace


# ---------------------------------------------------------------------------
# 动作记录与目录
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ActionContext:
    """执行动作所需的外部能力；由服务层按请求构造。"""

    use_memory: bool = False
    tools_enabled: bool = False          # 模型工具主开关（AGENT_ENABLE_TOOLS）
    exec_enabled: bool = False           # AGENT_ENABLE_EXEC_TOOL
    web_enabled: bool = False            # AGENT_ENABLE_WEB_TOOL
    timeout_limit: float | None = None   # 请求剩余时间（秒）；None 表示不限
    search_memory: Callable[[str], list[dict[str, Any]]] | None = None
    memory_payload: Callable[[list[dict[str, Any]]], tuple[bool, str]] | None = None


@dataclass(frozen=True)
class Action:
    """一个可用动作：schema、校验、执行与能力开关集中在同一条目里。"""

    name: str
    schema: dict[str, Any]
    validate: Callable[[dict[str, Any]], dict[str, Any]]   # 合法返回 kwargs，非法抛 ValueError
    execute: Callable[..., tuple[bool, str]]
    mutates: bool = False          # 变更类动作：做重复指纹与工件核验
    artifact: bool = False         # 成功后核对沙箱内工件（文件类写操作）
    budget_key: str | None = None  # 需要请求级时钟的动作键（仅 exec_terminal）
    arguments_version: int = 1
    result_version: int = 1

    @property
    def function_schema(self) -> dict[str, Any]:
        return {"type": "function", "function": self.schema}


# ---------------------------------------------------------------------------
# 执行函数（自 service.py 迁入，行为不变）
# ---------------------------------------------------------------------------

_EXEC_DENIED_EXE = frozenset({
    "rm", "rmdir", "del", "erase", "format", "mkfs", "dd",
    "shutdown", "reboot", "halt", "poweroff", "init",
    "sudo", "su", "doas", "runas",
    "chmod", "chown", "chgrp", "passwd", "useradd", "userdel", "usermod",
    "systemctl", "service", "kill", "killall", "pkill", "taskkill",
    "reg", "regedit", "bcdedit", "diskpart", "takeown", "icacls",
    "at", "schtasks", "wmic", "cmd", "powershell", "pwsh", "bash", "sh", "zsh",
})
_EXEC_MAX_TIMEOUT = 60
_EXEC_DEFAULT_TIMEOUT = 15
_EXEC_OUTPUT_LIMIT = 8000
_READ_FILE_LIMIT = 8000
_LIST_DIR_LIMIT = 200
_WEB_FETCH_LIMIT = 8000
_WEB_FETCH_TIMEOUT = 15
_WEB_FETCH_MAX_BYTES = 256 * 1024
_SEARCH_FILES_LIMIT = 200
_SEARCH_CONTENT_LIMIT = 200
_SEARCH_CONTENT_SCAN_LIMIT = 3000
_WRITE_FILE_LIMIT = 64 * 1024

_MUTATING_ARTIFACT_ACTIONS = frozenset({"write_file", "edit_file", "delete_file"})


def exec_root() -> str:
    """沙箱根目录，可用 AGENT_EXEC_CWD 覆盖，默认进程工作目录。"""
    return os.path.abspath(os.getenv("AGENT_EXEC_CWD") or os.getcwd())


# 兼容别名：service.py 与既有测试按旧名引用沙箱根目录函数
_exec_root = exec_root


def mutating_actions() -> frozenset[str]:
    """变更类动作名集合（过去硬编码在 service.py 的 {'write_file', ...}）。"""
    return frozenset(action.name for action in ACTIONS if action.mutates)


def artifact_actions() -> frozenset[str]:
    """成功后需要核对沙箱工件的写动作名集合。"""
    return frozenset(action.name for action in ACTIONS if action.artifact)


def _strip_wrapping_quotes(token: str) -> str:
    """去掉一对包裹性引号。

    Windows 下 shlex 以 posix=False 分词时会保留反斜杠（路径必需），但不会剥离
    引号，导致可执行文件带着字面引号而找不到，因此需要手动剥离外层引号。
    """
    if len(token) >= 2 and token[0] == token[-1] and token[0] in ('"', "'"):
        return token[1:-1]
    return token


def _sandbox_resolve(path: str, root: str) -> str | None:
    try:
        return str(get_workspace().resolve(path)) if root == exec_root() else None
    except (WorkspaceError, OSError, ValueError):
        return None


def _exec_terminal(command: str, timeout: int | None = None, *,
                   require_success: bool = False) -> tuple[bool, str]:
    """在沙箱根目录中执行单条命令（不使用 shell）。

    Returns:
        (是否真正执行了命令, 回传给模型的文本结果)
    """
    cmd = (command or "").strip()
    if not cmd:
        return False, "命令为空。"
    if len(cmd) > 2000:
        return False, "命令过长（上限 2000 字符）。"
    for ch in (";", "&", "|", "`", "\n", "\r"):
        if ch in cmd:
            return False, "不支持命令链、管道与命令替换（; & | `），请改为单条命令。"

    try:
        parts = [_strip_wrapping_quotes(tok)
                 for tok in shlex.split(cmd, posix=(os.name != "nt"))]
    except ValueError:
        return False, "命令解析失败：引号不匹配。"
    if not parts or not parts[0]:
        return False, "命令为空。"

    exe = os.path.splitext(os.path.basename(parts[0]))[0].lower()
    if exe in _EXEC_DENIED_EXE:
        return False, f"命令 {exe!r} 属于危险命令，已拒绝执行。"

    root = exec_root()
    for arg in parts[1:]:
        if os.path.isabs(arg) or arg.startswith(".."):
            try:
                target = os.path.abspath(os.path.join(root, arg))
                if os.path.commonpath([target, root]) != root:
                    return False, f"拒绝访问沙箱目录之外的路径：{arg}"
            except ValueError:
                return False, f"拒绝访问沙箱目录之外的路径：{arg}"

    seconds = _EXEC_DEFAULT_TIMEOUT if timeout is None else int(timeout)
    # A cwd restriction and blacklist are not an OS sandbox. Accept only exact
    # commands pre-authorized by the host, never arbitrary model-written code.
    try:
        approved = json.loads(os.getenv("AGENT_EXEC_ALLOWED_COMMANDS", "[]"))
        if not isinstance(approved, list) or not all(isinstance(item, str) for item in approved):
            raise ValueError("invalid allowlist")
        allowed_parts = [[_strip_wrapping_quotes(t) for t in shlex.split(item, posix=(os.name != "nt"))] for item in approved]
    except (ValueError, TypeError):
        return False, "主机命令许可配置无效，已拒绝执行。"
    if parts not in allowed_parts:
        return False, "此命令未获主机预先许可，已拒绝执行。"
    seconds = max(1, min(seconds, _EXEC_MAX_TIMEOUT))
    try:
        done = subprocess.run(
            parts, cwd=root, timeout=seconds, shell=False,
            capture_output=True, text=True, errors="replace",
        )
    except subprocess.TimeoutExpired:
        return False, f"命令超时（超过 {seconds} 秒），已终止。"
    except FileNotFoundError:
        return False, f"找不到可执行文件：{parts[0]}"
    except OSError as exc:
        return False, f"执行失败：{exc}"

    output = (done.stdout or "") + (done.stderr or "")
    if len(output) > _EXEC_OUTPUT_LIMIT:
        output = output[:_EXEC_OUTPUT_LIMIT] + "\n...（输出已截断）"
    status = "成功" if done.returncode == 0 else f"失败（退出码 {done.returncode}）"
    return (done.returncode == 0 if require_success else True), f"[沙箱根目录 {root}]\n执行{status}：{cmd}\n{output}".strip()


def _read_file(path: str) -> tuple[bool, str]:
    try:
        item = get_workspace().read(path)
        data = item['content']
        if len(data) > _READ_FILE_LIMIT:
            data = data[:_READ_FILE_LIMIT] + '\n...（文件内容已截断）'
        return True, f"[文件 {item['path']}]\n{data}"
    except (WorkspaceError, OSError) as exc:
        return False, str(exc) if isinstance(exc, WorkspaceError) else '读取失败。'


def _list_directory(path: str | None) -> tuple[bool, str]:
    try:
        item = get_workspace().listing(path or '.', limit=_LIST_DIR_LIMIT)
        lines = [('d' if e['kind'] == 'directory' else 'f') + ' ' + e['name'] for e in item['entries']]
        if item['next_offset'] is not None:
            lines.append('...（目录已截断）')
        return True, f"[目录 {path or '.'}]\n" + '\n'.join(lines)
    except (WorkspaceError, OSError) as exc:
        return False, str(exc) if isinstance(exc, WorkspaceError) else '列目录失败。'


def _get_current_datetime() -> tuple[bool, str]:
    import datetime as _dt
    now = _dt.datetime.now()
    utc = _dt.datetime.now(_dt.timezone.utc)
    return True, (f"本地时间：{now.isoformat(timespec='seconds')}\n"
                  f"UTC 时间：{utc.isoformat(timespec='seconds')}\n"
                  f"时间戳：{int(now.timestamp())}")


_ALLOWED_BINOPS = (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow)


def _safe_eval_node(node):
    if isinstance(node, ast.Expression):
        return _safe_eval_node(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        return (+1 if isinstance(node.op, ast.UAdd) else -1) * _safe_eval_node(node.operand)
    if isinstance(node, ast.BinOp) and isinstance(node.op, _ALLOWED_BINOPS):
        left = _safe_eval_node(node.left)
        right = _safe_eval_node(node.right)
        if isinstance(node.op, ast.Add):
            return left + right
        if isinstance(node.op, ast.Sub):
            return left - right
        if isinstance(node.op, ast.Mult):
            return left * right
        if isinstance(node.op, ast.Div):
            return left / right
        if isinstance(node.op, ast.FloorDiv):
            return left // right
        if isinstance(node.op, ast.Mod):
            return left % right
        if isinstance(node.op, ast.Pow):
            return left ** right
    raise ValueError("不支持的表达式（仅允许数字与 + - * / % ** // 及括号）")


def _calculate(expression: str) -> tuple[bool, str]:
    expr = (expression or "").strip()
    if not expr:
        return False, "表达式为空。"
    if len(expr) > 500:
        return False, "表达式过长（上限 500 字符）。"
    if any(ch.isalpha() for ch in expr):
        return False, "仅支持数字与运算符（+ - * / % ** // 及括号），不允许变量或函数调用。"
    try:
        result = _safe_eval_node(ast.parse(expr, mode="eval"))
    except (ValueError, SyntaxError, ZeroDivisionError) as exc:
        return False, f"计算失败：{exc}"
    if isinstance(result, float) and result.is_integer():
        result = int(result)
    return True, f"{expr} = {result}"


def _search_files(pattern: str, path: str | None = None) -> tuple[bool, str]:
    root = exec_root()
    base = _sandbox_resolve(path or ".", root)
    if base is None:
        return False, f"拒绝访问沙箱目录之外的路径：{path}"
    if not os.path.isdir(base):
        return False, f"目录不存在：{path or '.'}"
    pattern = (pattern or "").strip()
    if not pattern:
        return False, "搜索模式为空。"
    if len(pattern) > 200 or ".." in pathlib.PureWindowsPath(pattern).parts:
        return False, "搜索模式无效或过长（上限 200 字符）。"
    try:
        matches = [os.path.relpath(str(p), root)
                   for p in pathlib.Path(base).glob(pattern)
                   if _sandbox_resolve(str(p), root) is not None][:_SEARCH_FILES_LIMIT+1]
    except (ValueError, OSError) as exc:
        return False, f"搜索失败：{exc}"
    matches.sort()
    truncated = len(matches) > _SEARCH_FILES_LIMIT
    if truncated:
        matches = matches[:_SEARCH_FILES_LIMIT]
    if not matches:
        return True, f"[搜索 {pattern}] 未找到匹配。"
    text = "\n".join(matches)
    if truncated:
        text += f"\n...（仅显示前 {_SEARCH_FILES_LIMIT} 项）"
    return True, f"[搜索 {pattern}]\n{text}"


def _search_content(pattern: str, path: str | None = None, glob: str = "*",
                    context: int = 0, case_sensitive: bool = False) -> tuple[bool, str]:
    """在沙箱内按正则搜索文件内容（grep），可限定文件名 glob 与上下文行数。"""
    root = exec_root()
    base = _sandbox_resolve(path or ".", root)
    if base is None:
        return False, f"拒绝访问沙箱目录之外的路径：{path}"
    if not os.path.isdir(base):
        return False, f"目录不存在：{path or '.'}"
    pattern = (pattern or "").strip()
    if not pattern:
        return False, "搜索内容为空。"
    if len(pattern) > 500:
        return False, "搜索内容过长（上限 500 字符）。"
    try:
        regex = re.compile(pattern, 0 if case_sensitive else re.IGNORECASE)
    except re.error as exc:
        return False, f"正则表达式无效：{exc}"
    glob = glob or "*"
    context = max(0, min(int(context or 0), 5))
    results: list[str] = []
    scanned = 0
    for dirpath, _dirnames, filenames in os.walk(base):
        _dirnames[:] = [d for d in _dirnames if _sandbox_resolve(os.path.join(dirpath, d), root) is not None]
        if len(results) >= _SEARCH_CONTENT_LIMIT:
            break
        for name in filenames:
            if len(results) >= _SEARCH_CONTENT_LIMIT:
                break
            if not fnmatch.fnmatch(name, glob):
                continue
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root)
            if _sandbox_resolve(full, root) is None:
                continue
            try:
                with open(full, "rb") as fb:
                    if b"\x00" in fb.read(1024):
                        continue  # 跳过二进制文件
                with open(full, "r", encoding="utf-8", errors="replace") as fh:
                    lines = fh.read(_WRITE_FILE_LIMIT).splitlines()
            except OSError:
                continue
            scanned += 1
            if scanned > _SEARCH_CONTENT_SCAN_LIMIT:
                break
            for i, line in enumerate(lines):
                if regex.search(line):
                    lo = max(0, i - context)
                    hi = min(len(lines), i + context + 1)
                    snippet = []
                    for j in range(lo, hi):
                        marker = ">" if j == i else " "
                        snippet.append(f"{marker}{j + 1}: {lines[j]}")
                    results.append(f"{rel}\n  " + "\n  ".join(snippet))
                    if len(results) >= _SEARCH_CONTENT_LIMIT:
                        break
    if not results:
        return True, f"[搜索内容 {pattern}] 未找到匹配。"
    text = "\n".join(results)
    if len(results) >= _SEARCH_CONTENT_LIMIT:
        text += f"\n...（仅显示前 {_SEARCH_CONTENT_LIMIT} 项）"
    return True, f"[搜索内容 {pattern} | glob={glob} | 扫描 {scanned} 个文件]\n{text}"


def _write_file(path: str, content: str, overwrite: bool = False) -> tuple[bool, str]:
    try:
        workspace = get_workspace()
        target = workspace.resolve(path)
        expected = None
        if target.exists():
            if not overwrite:
                return False, '文件已存在，如需覆盖请设置 overwrite=true，或修改写入路径。'
            expected = workspace.read(path)['revision']
        workspace.write(path, content, expected)
        return True, f'已写入 {path}（{len(content)} 字符）。'
    except (WorkspaceError, OSError) as exc:
        return False, str(exc) if isinstance(exc, WorkspaceError) else '写入失败。'


def _edit_file(path: str, old_string: str, new_string: str,
               replace_all: bool = False) -> tuple[bool, str]:
    if not isinstance(old_string, str) or not old_string:
        return False, 'old_string 不能为空。'
    if not isinstance(new_string, str):
        return False, 'new_string 必须是字符串。'
    try:
        workspace = get_workspace()
        item = workspace.read(path)
        data = item['content']
        count = data.count(old_string)
        if not count:
            return False, '未在文件中找到 old_string。'
        if count > 1 and not replace_all:
            return False, f'old_string 出现 {count} 次，存在歧义；请提供更具体的字符串，或设置 replace_all=true。'
        content = data.replace(old_string, new_string) if replace_all else data.replace(old_string, new_string, 1)
        workspace.write(path, content, item['revision'])
        return True, f'已更新 {path}（替换 {count if replace_all else 1} 处）。'
    except (WorkspaceError, OSError) as exc:
        return False, str(exc) if isinstance(exc, WorkspaceError) else '更新失败。'


def _delete_file(path: str) -> tuple[bool, str]:
    try:
        workspace = get_workspace()
        item = workspace.read(path)
        workspace.delete(path, item['revision'])
        return True, f'已删除文件 {item["path"]}；可从侧栏变更记录恢复。'
    except (WorkspaceError, OSError) as exc:
        return False, str(exc) if isinstance(exc, WorkspaceError) else '删除失败。'


def _web_fetch(url: str, timeout: float | None = None) -> tuple[bool, str]:
    """抓取网页文本；timeout 取抓取上限与请求剩余时间的较小值。"""
    url = (url or "").strip()
    if not url:
        return False, "URL 为空。"
    if len(url) > 2000:
        return False, "URL 过长。"
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return False, "仅支持 http/https 链接。"
    from src.security.outbound import is_external_url_safe

    if not is_external_url_safe(url):
        return False, "出于安全策略，禁止访问内网、回环或链路本地地址（SSRF 防护）。"
    effective = _WEB_FETCH_TIMEOUT if timeout is None else max(1.0, min(_WEB_FETCH_TIMEOUT, float(timeout)))
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "MOKOA-Agent/1.0"})
        with urllib.request.urlopen(req, timeout=effective) as resp:
            raw = resp.read(_WEB_FETCH_MAX_BYTES + 1)
            ctype = resp.headers.get("Content-Type", "")
    except (urllib.error.URLError, ValueError, OSError) as exc:
        return False, f"抓取失败：{exc}"
    if len(raw) > _WEB_FETCH_MAX_BYTES:
        raw = raw[:_WEB_FETCH_MAX_BYTES]
    text = raw.decode("utf-8", errors="replace")
    text = "\n".join(line.strip() for line in text.splitlines() if line.strip())
    if len(text) > _WEB_FETCH_LIMIT:
        text = text[:_WEB_FETCH_LIMIT] + "\n...（网页内容已截断）"
    return True, f"[抓取 {url} | {ctype}]\n{text}"


# ---------------------------------------------------------------------------
# 参数校验（自 service.py 迁入；非法抛 ValueError，合法返回 kwargs）
# ---------------------------------------------------------------------------

def _v_search_memory(args: dict) -> dict:
    if set(args) != {"query"}:
        raise ValueError("invalid query")
    query = args["query"]
    if not isinstance(query, str) or not 1 <= len(query.strip()) <= 2000:
        raise ValueError("invalid query")
    return {"query": query}


def _v_exec_terminal(args: dict) -> dict:
    if set(args) - {"command", "timeout"} or set(args) == set():
        raise ValueError("invalid command")
    command, timeout = args.get("command"), args.get("timeout")
    if (not isinstance(command, str) or not command.strip()
            or timeout is not None and (type(timeout) is not int or not 1 <= timeout <= _EXEC_MAX_TIMEOUT)):
        raise ValueError("invalid command")
    return {"command": command, "timeout": timeout}


def _v_read_file(args: dict) -> dict:
    if set(args) != {"path"} or not isinstance(args["path"], str) or not args["path"].strip():
        raise ValueError("invalid tool")
    return {"path": args["path"]}


def _v_list_directory(args: dict) -> dict:
    if set(args) - {"path"} or not isinstance(args.get("path"), (str, type(None))):
        raise ValueError("invalid tool")
    return {"path": args.get("path")}


def _v_get_current_datetime(args: dict) -> dict:
    if args:
        raise ValueError("invalid tool")
    return {}


def _v_calculate(args: dict) -> dict:
    if set(args) != {"expression"} or not isinstance(args["expression"], str) or not args["expression"].strip():
        raise ValueError("invalid tool")
    return {"expression": args["expression"]}


def _v_search_files(args: dict) -> dict:
    if set(args) - {"pattern", "path"} or not isinstance(args.get("pattern"), str) or not args["pattern"].strip():
        raise ValueError("invalid tool")
    if not isinstance(args.get("path"), (str, type(None))):
        raise ValueError("invalid path")
    return {"pattern": args["pattern"], "path": args.get("path")}


def _v_search_content(args: dict) -> dict:
    if set(args) - {"pattern", "path", "glob", "context", "case_sensitive"}:
        raise ValueError("invalid tool")
    if not isinstance(args.get("pattern"), str) or not args["pattern"].strip():
        raise ValueError("invalid tool")
    if (not isinstance(args.get("path"), (str, type(None)))
            or not isinstance(args.get("glob", "*"), str)
            or type(args.get("context", 0)) is not int
            or not isinstance(args.get("case_sensitive", False), bool)):
        raise ValueError("invalid search options")
    return {"pattern": args["pattern"], "path": args.get("path"),
            "glob": args.get("glob", "*"), "context": args.get("context", 0),
            "case_sensitive": bool(args.get("case_sensitive", False))}


def _v_write_file(args: dict) -> dict:
    if set(args) - {"path", "content", "overwrite"}:
        raise ValueError("invalid tool")
    if not isinstance(args.get("path"), str) or not isinstance(args.get("content"), str):
        raise ValueError("invalid tool")
    return {"path": args["path"], "content": args["content"],
            "overwrite": bool(args.get("overwrite", False))}


def _v_edit_file(args: dict) -> dict:
    if set(args) - {"path", "old_string", "new_string", "replace_all"}:
        raise ValueError("invalid tool")
    if (not isinstance(args.get("path"), str) or not isinstance(args.get("old_string"), str)
            or not isinstance(args.get("new_string"), str)):
        raise ValueError("invalid tool")
    return {"path": args["path"], "old_string": args["old_string"],
            "new_string": args["new_string"], "replace_all": bool(args.get("replace_all", False))}


def _v_delete_file(args: dict) -> dict:
    if set(args) != {"path"} or not isinstance(args["path"], str):
        raise ValueError("invalid tool")
    return {"path": args["path"]}


def _v_web_fetch(args: dict) -> dict:
    if set(args) != {"url"} or not isinstance(args["url"], str) or not args["url"].strip():
        raise ValueError("invalid url")
    return {"url": args["url"]}


# ---------------------------------------------------------------------------
# 动作 schema（自 service.py 常量迁入）
# ---------------------------------------------------------------------------

_SCHEMA_SEARCH_MEMORY = {
    "name": "search_memory", "description": "按新的关键词补充检索当前用户的记忆。",
    "parameters": {"type": "object", "properties": {"query": {"type": "string"}},
                   "required": ["query"], "additionalProperties": False},
}
_SCHEMA_EXEC_TERMINAL = {
    "name": "exec_terminal",
    "description": (
        "在受控沙箱目录（项目根目录）内执行一条终端命令，返回退出码与输出。"
        "仅支持单条命令，不支持管道 |、重定向和命令链（;、&&）。"
        "可用于列目录、查看文件、运行测试与脚本、查看 git 状态等。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "要执行的单条命令，例如 git status"},
            "timeout": {"type": "integer", "minimum": 1, "maximum": 60,
                        "description": "超时秒数，默认 15，最大 60"},
        },
        "required": ["command"],
        "additionalProperties": False,
    },
}
_SCHEMA_READ_FILE = {
    "name": "read_file",
    "description": "读取沙箱目录（项目根目录）内某个文本文件的内容并返回。用于查看源码、配置、日志等。只读，不会修改文件。",
    "parameters": {"type": "object",
                   "properties": {"path": {"type": "string", "description": "相对沙箱根目录的路径，例如 src/agent/service.py"}},
                   "required": ["path"], "additionalProperties": False},
}
_SCHEMA_LIST_DIR = {
    "name": "list_directory",
    "description": "列出沙箱目录（项目根目录）内某个目录的内容，区分目录(d)/文件(f)。只读。",
    "parameters": {"type": "object",
                   "properties": {"path": {"type": "string", "description": "相对沙箱根目录的目录路径，留空或 '.' 表示根目录"}},
                   "required": [], "additionalProperties": False},
}
_SCHEMA_DATETIME = {
    "name": "get_current_datetime",
    "description": "获取当前日期与时间（本地与 UTC），用于需要「今天/现在/几天后」等时间信息的场景。",
    "parameters": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
}
_SCHEMA_CALCULATE = {
    "name": "calculate",
    "description": "安全地计算一个纯数值数学表达式，支持 + - * / % ** // 与括号。用于日期差值、数量统计等需要精确计算的场景。不支持变量或函数调用。",
    "parameters": {"type": "object",
                   "properties": {"expression": {"type": "string", "description": "数学表达式，例如 365 * 3 + (12 * 30)"}},
                   "required": ["expression"], "additionalProperties": False},
}
_SCHEMA_WEB_FETCH = {
    "name": "web_fetch",
    "description": "抓取一个 http/https 网页的文本内容并返回（仅文本，自动截断）。用于查阅在线文档、网页资料等。需服务端开启联网。",
    "parameters": {"type": "object",
                   "properties": {"url": {"type": "string", "description": "以 http:// 或 https:// 开头的完整网址"}},
                   "required": ["url"], "additionalProperties": False},
}
_SCHEMA_SEARCH_FILES = {
    "name": "search_files",
    "description": "在沙箱目录内按文件名模式递归搜索文件/目录，支持 glob（如 *.py、src/**/*.ts）。用于定位文件位置。只读。",
    "parameters": {"type": "object",
                   "properties": {
                       "pattern": {"type": "string", "description": "glob 模式，例如 *.py 或 src/**/*.ts"},
                       "path": {"type": "string", "description": "相对沙箱根目录的起始目录，留空或 '.' 表示根目录"}},
                   "required": ["pattern"], "additionalProperties": False},
}
_SCHEMA_SEARCH_CONTENT = {
    "name": "search_content",
    "description": "在沙箱目录内的文件中按正则搜索文本内容（grep），可限定文件名 glob 与上下文行数。用于查找代码、配置中的关键字。只读。",
    "parameters": {"type": "object",
                   "properties": {
                       "pattern": {"type": "string", "description": "正则表达式，要搜索的文本内容"},
                       "path": {"type": "string", "description": "相对沙箱根目录的起始目录，留空或 '.' 表示根目录"},
                       "glob": {"type": "string", "description": "文件名过滤（glob），例如 *.py，默认 *（全部文件）"},
                       "context": {"type": "integer", "description": "匹配行上下各显示的上下文行数，默认 0，最大 5"},
                       "case_sensitive": {"type": "boolean", "description": "是否区分大小写，默认 false（不区分）"}},
                   "required": ["pattern"], "additionalProperties": False},
}
_SCHEMA_WRITE_FILE = {
    "name": "write_file",
    "description": "在沙箱目录内写入一个文本文件。默认仅创建新文件；若文件已存在会被拒绝，需设置 overwrite=true 覆盖。用于生成脚本、配置或草稿。",
    "parameters": {"type": "object",
                   "properties": {
                       "path": {"type": "string", "description": "相对沙箱根目录的文件路径，例如 scripts/run.py"},
                       "content": {"type": "string", "description": "要写入的完整文本内容"},
                       "overwrite": {"type": "boolean", "description": "是否覆盖已存在的文件，默认 false"}},
                   "required": ["path", "content"], "additionalProperties": False},
}
_SCHEMA_EDIT_FILE = {
    "name": "edit_file",
    "description": "在沙箱目录内某个已有的文本文件中，将 old_string 替换为 new_string。若 old_string 出现多次且未设置 replace_all，会被拒绝以避免歧义。用于精确修改文件。",
    "parameters": {"type": "object",
                   "properties": {
                       "path": {"type": "string", "description": "相对沙箱根目录的已存在文件路径"},
                       "old_string": {"type": "string", "description": "要被替换的原文本（不能为空）"},
                       "new_string": {"type": "string", "description": "替换后的新文本"},
                       "replace_all": {"type": "boolean", "description": "是否替换全部匹配，默认 false（仅替换首个，多次出现会报错）"}},
                   "required": ["path", "old_string", "new_string"], "additionalProperties": False},
}
_SCHEMA_DELETE_FILE = {
    "name": "delete_file", "description": "删除用户明确指定的工作区文本文件；保留恢复记录。不支持目录删除，不用于遗忘记忆。",
    "parameters": {"type": "object", "properties": {"path": {"type": "string"}},
                   "required": ["path"], "additionalProperties": False},
}


SEARCH_MEMORY = Action("search_memory", _SCHEMA_SEARCH_MEMORY, _v_search_memory,
                       lambda query, ctx=None: _ACTION_UNSET)
EXEC_TERMINAL = Action("exec_terminal", _SCHEMA_EXEC_TERMINAL, _v_exec_terminal,
                       _exec_terminal, mutates=True, budget_key="exec")
READ_FILE = Action("read_file", _SCHEMA_READ_FILE, _v_read_file, _read_file)
LIST_DIRECTORY = Action("list_directory", _SCHEMA_LIST_DIR, _v_list_directory, _list_directory)
GET_CURRENT_DATETIME = Action("get_current_datetime", _SCHEMA_DATETIME,
                              _v_get_current_datetime, _get_current_datetime)
CALCULATE = Action("calculate", _SCHEMA_CALCULATE, _v_calculate, _calculate)
WEB_FETCH = Action("web_fetch", _SCHEMA_WEB_FETCH, _v_web_fetch, _web_fetch)
SEARCH_FILES = Action("search_files", _SCHEMA_SEARCH_FILES, _v_search_files, _search_files)
SEARCH_CONTENT = Action("search_content", _SCHEMA_SEARCH_CONTENT, _v_search_content, _search_content)
WRITE_FILE = Action("write_file", _SCHEMA_WRITE_FILE, _v_write_file, _write_file,
                    mutates=True, artifact=True)
EDIT_FILE = Action("edit_file", _SCHEMA_EDIT_FILE, _v_edit_file, _edit_file,
                   mutates=True, artifact=True)
DELETE_FILE = Action("delete_file", _SCHEMA_DELETE_FILE, _v_delete_file, _delete_file,
                     mutates=True, artifact=True)

_ACTION_UNSET = (False, "dispatch_action 必须注入 ActionContext 后调用。")


ACTIONS: tuple[Action, ...] = (
    SEARCH_MEMORY, EXEC_TERMINAL, READ_FILE, LIST_DIRECTORY, GET_CURRENT_DATETIME,
    CALCULATE, SEARCH_FILES, SEARCH_CONTENT, WRITE_FILE, EDIT_FILE, DELETE_FILE, WEB_FETCH,
)
_ACTION_BY_NAME: dict[str, Action] = {action.name: action for action in ACTIONS}


def get_action(name: str) -> Action | None:
    """按名称取动作；未知动作返回 None。"""
    return _ACTION_BY_NAME.get(name)


def actions_available(ctx: ActionContext) -> tuple[Action, ...]:
    """按能力开关返回当前可用的动作序列（顺序与原 service.py 注册顺序一致）。

    search_memory 的回调（search_memory/memory_payload）在执行时才需要，
    注册模型工具列表时不要求已注入。
    """
    available: list[Action] = []
    for action in ACTIONS:
        if action.name == "search_memory":
            if ctx.use_memory and ctx.tools_enabled:
                available.append(action)
        elif action.name == "exec_terminal":
            if ctx.exec_enabled and ctx.tools_enabled:
                available.append(action)
        elif action.name == "web_fetch":
            if ctx.web_enabled and ctx.tools_enabled:
                available.append(action)
        elif ctx.tools_enabled:
            available.append(action)
    return tuple(available)


def tools_for_model(ctx: ActionContext) -> list[dict[str, Any]]:
    """模型工具列表：与 actions_available 同一目录，同一能力开关。"""
    return [action.function_schema for action in actions_available(ctx)]


def audit_entry(name: str, args: dict) -> dict[str, str]:
    """对外暴露的安全摘要：仅白名单键、脱敏后截断（原 _tools 的 safe_args 逻辑）。"""
    from src.security.service import redact_sensitive_text
    safe_args: dict[str, str] = {}
    for key in ("path", "pattern", "query", "expression", "url"):
        value = args.get(key)
        if isinstance(value, str) and not (key == "path" and pathlib.Path(value).is_absolute()):
            safe_args[key] = redact_sensitive_text(value)[:200]
    return safe_args


def mutation_fingerprint(name: str, args: dict) -> str:
    """变更类动作的去重指纹（原 _tools 的 sha256 逻辑）。"""
    import hashlib
    return hashlib.sha256(json.dumps([name, args], sort_keys=True,
        ensure_ascii=False).encode()).hexdigest()


def dispatch_action(name: str, args: dict, ctx: ActionContext) -> tuple[bool, str, str | None]:
    """统一动作分发入口：模型 tool_call 与 UI/API 直调都走这里。

    Returns:
        (ok, content, error_code)。未知动作返回 (False, 提示, "tool_unavailable")；
        参数非法返回 (False, 提示, "invalid_arguments")；不抛异常。
    """
    action = _ACTION_BY_NAME.get(name)
    if action is None:
        return False, "不支持的工具：未获允许或当前不可用。", "tool_unavailable"
    # 能力开关统一裁决：目录里可用才允许执行（与模型可注册的集合一致）
    if action not in actions_available(ctx):
        return False, "不支持的工具：未获允许或当前不可用。", "tool_unavailable"
    try:
        kwargs = action.validate(args)
        if action.name == "search_memory":
            # 契约：与其他动作一致返回三元组 (ok, content, error_code)。
            if ctx.search_memory is None or ctx.memory_payload is None:
                return False, "记忆检索上下文未注入，无法执行。", "tool_unavailable"
            hits = ctx.search_memory(kwargs["query"])
            ok, content = ctx.memory_payload(hits)
        elif action.budget_key == "exec":
            # 请求级时钟：超时 = 动作自身上限与调用方传入的剩余时间较小值
            # （调用方在 ActionContext.timeout_limit 里传 clamp 后的剩余秒数）。
            remaining = (kwargs.get("timeout") or _EXEC_DEFAULT_TIMEOUT)
            if ctx.timeout_limit is not None:
                remaining = min(remaining, ctx.timeout_limit)
            if remaining < 1:
                return False, "执行超时，已停止。", "timeout"
            ok, content = action.execute(kwargs["command"], int(max(1, remaining)), require_success=True)
        elif action.name == "web_fetch" and ctx.timeout_limit is not None:
            # 抓取超时同样受请求剩余时间约束（_web_fetch 内部再 clamp 到自身上限）
            ok, content = action.execute(kwargs["url"], float(ctx.timeout_limit))
        else:
            ok, content = action.execute(**kwargs)
        # 执行层失败兜底：与原 service._tools 的 error or "tool_failed" 口径一致
        return ok, content, ("tool_failed" if not ok else None)
    except (ValueError, KeyError, TypeError):
        return False, "工具参数无效，未完成操作。", "invalid_arguments"
    except OSError:
        return False, "工具执行失败，请检查输入及服务状态。", "tool_error"
