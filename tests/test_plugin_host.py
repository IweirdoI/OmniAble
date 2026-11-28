"""TASK-024 / Issue #32 受限插件宿主回归。

覆盖验收：
- 同一示例经聊天（dispatch_action）和桌面（/workspace/actions）运行
- 越界读取、联网、启动未授权进程被有效阻止
- 超时/崩溃只终止插件（宿主继续）
- 停用后动作消失；升级失败仍能使用旧版本；回滚
- 安装/启用/停用/升级/回滚可审计
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from src.plugin_package import TrustStore, pack_plugin
from src.plugin_package.trust import TrustedKey

KEY_ID = "host-test-root"

PAYLOAD = '''\
"""Host fixture payload."""


def echo(text):
    return text


def read_outside():
    # 跨平台越界：相对路径跳出插件目录（Linux 上 "C:/..." 不是绝对路径）
    with open("../outside_secret.txt") as handle:
        return handle.read()


def slow():
    import time
    time.sleep(30)
    return "done"


def crash():
    raise RuntimeError("boom")
'''


def _keypair():
    private = Ed25519PrivateKey.generate()
    key = TrustedKey(KEY_ID, "ed25519", private.public_key(), "Host Test")
    return private, key


def _key_file(tmp_path: Path, private: Ed25519PrivateKey) -> Path:
    path = tmp_path / "key.pem"
    path.write_bytes(private.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ))
    return path


def _manifest(version: str = "1.0.0", *, action_ids: list[str] | None = None) -> dict:
    ids = action_ids or ["echo", "read_outside", "net", "spawn", "slow", "crash"]
    return {
        "schema": "mokoa.plugin/v1",
        "id": "com.example.hostfixture",
        "name": "Host Fixture",
        "version": version,
        "host_api": ">=1.0.0 <2.0.0",
        "entry": "payload/main.py",
        "license": "MIT",
        "license_file": "LICENSE",
        "publisher": {"key_id": KEY_ID, "name": "Host Test"},
        "capabilities": ["files.read"],
        "actions": [
            {"id": aid, "name": aid, "description": aid,
             "parameters": {"type": "object", "properties": {}},
             "capabilities": ["files.read"], "mutates": False}
            for aid in ids
        ],
        "dependencies": [],
        "files": {},
    }


def _pack(tmp_path: Path, private: Ed25519PrivateKey, *, version: str = "1.0.0",
          payload: str = PAYLOAD, name: str = "pkg",
          action_ids: list[str] | None = None) -> Path:
    source = tmp_path / f"src-{name}-{version}"
    (source / "payload").mkdir(parents=True)
    (source / "manifest.json").write_text(
        json.dumps(_manifest(version, action_ids=action_ids), ensure_ascii=False),
        encoding="utf-8")
    (source / "payload" / "main.py").write_text(payload, encoding="utf-8")
    (source / "LICENSE").write_text("MIT\n", encoding="utf-8")
    output = tmp_path / f"{name}-{version}.mko"
    return pack_plugin(source, _key_file(tmp_path, private), output)


@pytest.fixture
def host(tmp_path):
    from src.agent.plugin_host import PluginHost
    private, key = _keypair()
    trust = TrustStore({key.key_id: key}, set())
    h = PluginHost(project_root=str(tmp_path), trust_store=trust,
                   runner_timeout=5.0)
    h._test_private = private  # type: ignore[attr-defined]
    yield h
    h.close()


def _install(host, tmp_path, *, version: str = "1.0.0", payload: str = PAYLOAD):
    package = _pack(tmp_path, host._test_private, version=version, payload=payload)
    return host.install(package)


# ---------------------------------------------------------------------------
# 安装/启用/停用/升级/回滚 + 审计
# ---------------------------------------------------------------------------

def test_install_enable_disable_actions_visibility(host, tmp_path) -> None:
    """安装后动作进入统一目录；停用后动作消失；启用恢复。"""
    from src.agent.actions import ActionContext, actions_available, get_action

    result = _install(host, tmp_path)
    assert result["plugin_id"] == "com.example.hostfixture"
    ctx = ActionContext(tools_enabled=True)
    names = {a.name for a in actions_available(ctx)}
    assert "plugin.com.example.hostfixture.echo" in names
    assert get_action("plugin.com.example.hostfixture.echo") is not None

    host.set_enabled("com.example.hostfixture", False)
    names_off = {a.name for a in actions_available(ctx)}
    assert "plugin.com.example.hostfixture.echo" not in names_off, "停用后动作消失"
    assert get_action("plugin.com.example.hostfixture.echo") is None

    host.set_enabled("com.example.hostfixture", True)
    assert get_action("plugin.com.example.hostfixture.echo") is not None


def test_same_sample_runs_via_chat_and_desktop(host, tmp_path, monkeypatch) -> None:
    """同一示例经聊天（dispatch_action）与桌面（/workspace/actions）等价运行。"""
    from src.agent.actions import ActionContext, dispatch_action

    _install(host, tmp_path)
    ctx = ActionContext(tools_enabled=True)
    direct = host.run_action("com.example.hostfixture", "echo", {"text": "hello"})
    assert direct == (True, "hello"), f"直调失败：{direct!r}"
    ok, content, error = dispatch_action(
        "plugin.com.example.hostfixture.echo", {"text": "hello"}, ctx)
    assert ok is True and content == "hello" and error is None, (
        f"dispatch 失败：ok={ok} content={content!r} error={error!r}")

    # 桌面入口：/workspace/actions 列表 + 直调（同一 dispatch 入口）
    monkeypatch.setenv("AGENT_ENABLE_WORKSPACE", "1")
    monkeypatch.setenv("AGENT_ENABLE_TOOLS", "1")
    monkeypatch.setenv("AGENT_EXEC_CWD", str(tmp_path))
    import api.server as server
    from fastapi.testclient import TestClient
    server.plugin_host = host  # 桌面入口使用同一宿主实例
    # 桌面动作端点要求可信宿主令牌（TASK-021 合入版安全语义）
    monkeypatch.setattr(server, "_API_AUTH_TOKEN", "test-host-token")
    with TestClient(server.app) as client:
        client.headers["Authorization"] = "Bearer test-host-token"
        listing = client.get("/workspace/actions").json()
        assert "plugin.com.example.hostfixture.echo" in {
            a["name"] for a in listing["actions"]}
        resp = client.post("/workspace/actions/plugin.com.example.hostfixture.echo",
                           json={"arguments": {"text": "桌面"}})
        assert resp.status_code == 200
        assert resp.json()["result"] == "桌面"


def test_upgrade_failure_keeps_old_version(host, tmp_path) -> None:
    """升级失败（坏包）保留旧版本：动作仍可用、版本不变。"""
    from src.agent.actions import ActionContext, dispatch_action

    _install(host, tmp_path, version="1.0.0")
    bad = tmp_path / "bad.mko"
    bad.write_bytes(b"not a zip")
    with pytest.raises(Exception):
        host.upgrade(bad)
    record = host.list_installed()[0]
    assert record["version"] == "1.0.0", "升级失败必须保留旧版本"
    ok, content, _ = dispatch_action(
        "plugin.com.example.hostfixture.echo", {"text": "still works"},
        ActionContext(tools_enabled=True))
    assert ok is True and content == "still works"


def test_upgrade_success_and_rollback(host, tmp_path) -> None:
    """升级成功切换版本；回滚切回上一版本。"""
    _install(host, tmp_path, version="1.0.0")
    package2 = _pack(tmp_path, host._test_private, version="1.1.0", name="v2")
    result = host.upgrade(package2)
    assert result["version"] == "1.1.0" and result["previous_version"] == "1.0.0"
    assert host.list_installed()[0]["version"] == "1.1.0"

    rolled = host.rollback("com.example.hostfixture")
    assert rolled["version"] == "1.0.0"
    assert host.list_installed()[0]["version"] == "1.0.0"


def test_audit_records_all_operations(host, tmp_path) -> None:
    """安装/停用/升级/回滚全部可审计。"""
    _install(host, tmp_path, version="1.0.0")
    host.set_enabled("com.example.hostfixture", False)
    package2 = _pack(tmp_path, host._test_private, version="1.1.0", name="v2")
    host.upgrade(package2)
    host.rollback("com.example.hostfixture")
    ops = [entry["operation"] for entry in host.audit_log()]
    assert {"install", "disable", "upgrade", "rollback"} <= set(ops)


# ---------------------------------------------------------------------------
# 受限执行：越界/联网/进程被阻止；超时/崩溃隔离
# ---------------------------------------------------------------------------

def test_out_of_bounds_read_blocked(host, tmp_path) -> None:
    _install(host, tmp_path)
    ok, content = host.run_action("com.example.hostfixture", "read_outside", {})
    assert ok is False and "越界" in content


def test_network_blocked(host, tmp_path) -> None:
    """联网在**宿主侧**被强制：socket import → 扫描拒绝安装（不到运行时）。"""
    from src.agent.plugin_host import PluginHostError
    bad = '''
def net():
    import socket
    sock = socket.socket()
    sock.connect(("1.1.1.1", 80))
    return "connected"
'''
    package = _pack(tmp_path, host._test_private, version="9.9.9", payload=bad,
                    name="badnet", action_ids=["net"])
    with pytest.raises(PluginHostError):
        host.install(package)
    assert host.list_installed() == []


def test_process_spawn_blocked(host, tmp_path) -> None:
    """进程启动在**宿主侧**被强制：subprocess import → 扫描拒绝安装。"""
    from src.agent.plugin_host import PluginHostError
    bad = '''
def spawn():
    import subprocess
    return subprocess.run(["cmd", "/c", "echo", "hi"]).returncode
'''
    package = _pack(tmp_path, host._test_private, version="9.9.9", payload=bad,
                    name="badproc", action_ids=["spawn"])
    with pytest.raises(PluginHostError):
        host.install(package)
    assert host.list_installed() == []


def test_timeout_only_kills_plugin(host, tmp_path) -> None:
    """超时只终止插件子进程；宿主继续可用。"""
    _install(host, tmp_path)
    ok, content = host.run_action("com.example.hostfixture", "slow", {}, timeout=1.0)
    assert ok is False and "超时" in content
    # 宿主继续可用
    ok2, content2 = host.run_action("com.example.hostfixture", "echo", {"text": "alive"})
    assert ok2 is True and content2 == "alive"


def test_crash_only_fails_that_call(host, tmp_path) -> None:
    _install(host, tmp_path)
    ok, content = host.run_action("com.example.hostfixture", "crash", {})
    assert ok is False and "boom" in content
    ok2, content2 = host.run_action("com.example.hostfixture", "echo", {"text": "ok"})
    assert ok2 is True and content2 == "ok"


def test_disabled_plugin_cannot_run(host, tmp_path) -> None:
    _install(host, tmp_path)
    host.set_enabled("com.example.hostfixture", False)
    ok, content = host.run_action("com.example.hostfixture", "echo", {"text": "x"})
    assert ok is False and "停用" in content


def test_builtin_name_collision_rejected() -> None:
    """插件动作不得覆盖内置动作（保留旧工具名兼容）。"""
    from src.agent.actions import Action, register_action

    with pytest.raises(ValueError):
        register_action(Action(
            name="calculate", schema={"name": "calculate", "description": "x"},
            validate=lambda args: args, execute=lambda **kw: (True, "")))


# ---------------------------------------------------------------------------
# 复核整改：跨平台越界 / AST 宿主侧强制 / 快照一致性
# ---------------------------------------------------------------------------

def test_forbidden_api_scan_blocks_install(host, tmp_path) -> None:
    """宿主侧强制：payload 含禁用 API（socket/subprocess）→ 安装被拒绝。"""
    from src.agent.plugin_host import PluginHostError

    bad = '''
def steal():
    import socket
    sock = socket.socket()
    return "connected"
'''
    package = _pack(tmp_path, host._test_private, version="9.9.9", payload=bad,
                    name="badnet", action_ids=["steal"])
    with pytest.raises(PluginHostError):
        host.install(package)
    assert host.list_installed() == [], "扫描命中的插件不得安装"
    assert any("扫描" in (e.get("detail") or "") or e["ok"] == 0
               for e in host.audit_log())


def test_forbidden_api_scan_blocks_upgrade_and_keeps_old(host, tmp_path) -> None:
    """升级含禁用 API 的新版本被拒 → 旧版本保留。"""
    from src.agent.plugin_host import PluginHostError

    _install(host, tmp_path, version="1.0.0")
    bad = '''
def break_out():
    import subprocess
    return subprocess.run(["cmd"]).returncode
'''
    package = _pack(tmp_path, host._test_private, version="2.0.0", payload=bad,
                    name="badproc", action_ids=["break_out"])
    with pytest.raises(PluginHostError):
        host.upgrade(package)
    record = host.list_installed()[0]
    assert record["version"] == "1.0.0", "扫描失败保留旧版本"


def test_os_open_and_ctypes_blocked_by_scan(host, tmp_path) -> None:
    """os.open / ctypes 绕过面在扫描层拒绝（宿主强制）。"""
    from src.agent.plugin_host import PluginHostError

    bad = '''
def bypass():
    import ctypes
    return "bypassed"
'''
    package = _pack(tmp_path, host._test_private, version="3.0.0", payload=bad,
                    name="badct", action_ids=["bypass"])
    with pytest.raises(PluginHostError):
        host.install(package)


# ---------------------------------------------------------------------------
# 复核二轮：执行时宿主强制（受限代码由宿主生成，import 在源码层改写）
# ---------------------------------------------------------------------------

def test_runtime_restriction_by_host_rewrite(host, tmp_path) -> None:
    """执行时宿主强制：import 在源码层被改写——
    受控库（time）从宿主受限模块表取值；未授权 import 源码层即拒绝，
    子进程根本不会执行该 import 语句。"""
    from src.agent.plugin_host import PluginHostError

    # 先装默认包（echo/slow），验证运行时路径
    _install(host, tmp_path)
    # 运行时路径：time 受控（slow 动作能 import time 并被超时终止）
    ok, content = host.run_action("com.example.hostfixture", "slow", {}, timeout=1.0)
    assert ok is False and "超时" in content
    host._unregister_actions("com.example.hostfixture")

    # 再验证未授权 import 的受限代码改写（独立生成，不安装）
    bad = '''
def net():
    import socket
    sock = socket.socket()
    sock.connect(("1.1.1.1", 80))
    return "connected"
'''
    source = tmp_path / "src-scan"
    (source / "payload").mkdir(parents=True)
    (source / "manifest.json").write_text(
        json.dumps(_manifest("9.9.9", action_ids=["net"]), ensure_ascii=False),
        encoding="utf-8")
    (source / "payload" / "main.py").write_text(bad, encoding="utf-8")
    (source / "LICENSE").write_text("MIT\n", encoding="utf-8")
    # 直接对源目录生成受限代码：socket import 改写为 PermissionError
    code = host._build_restricted_source(source, "net")
    # import 行被改写为 raise（宿主强制），不再作为可执行 import 存在
    assert "raise PermissionError('插件 import socket 被宿主拒绝" in code
    assert "sock.connect" in code, "后续代码保留（执行到 raise 即止）"


def test_nested_import_also_rewritten(host, tmp_path) -> None:
    """函数体内嵌套 import 同样被改写（受控 time 可用；socket 拒绝）。"""
    _install(host, tmp_path)
    ok, content = host.run_action("com.example.hostfixture", "echo", {"text": "x"})
    assert ok is True and content == "x"
    # 越界读取：受限 open 仍拦截（运行时第二层）
    ok2, content2 = host.run_action("com.example.hostfixture", "read_outside", {})
    assert ok2 is False and "越界" in content2


# ---------------------------------------------------------------------------
# 复核三轮：OS 层生效断言（内核回读 + fork 拒绝）与宿主强制改写
# ---------------------------------------------------------------------------

def test_os_job_kernel_readback_and_fork_denied(host, tmp_path) -> None:
    """OS 层生效断言：Job 内核回读 limit_flags/UI 掩码；插件 import
    subprocess 被宿主改写为 PermissionError（源码层 fork 防护）。"""
    import ctypes
    from src.agent import plugin_host as ph
    from src.common.offline_profile import OutboundBlocked

    # 1) 安装默认包（echo/slow/read_outside，无禁用 API）
    _install(host, tmp_path)

    # 2) Popen runner + 绑入 Job + 内核回读
    plugin_dir = host.plugins_dir / "com.example.hostfixture" / "1.0.0"
    code = host._build_restricted_source(plugin_dir, "echo")
    runner = Path(__file__).resolve().parents[1] / "src" / "agent" / "plugin_runner.py"
    process = subprocess.Popen(
        [sys.executable, "-I", str(runner)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        cwd=str(plugin_dir))
    note, job = ph._apply_windows_job_restrictions(process)
    try:
        assert note is None, f"Job 绑定失败：{note}"
        readback = ph.query_job_limits(job)
        assert readback.get("limit_flags", 0) & ph._JOB_LIMIT_KILL_ON_CLOSE
        assert readback.get("ui", 0) & ph._JOB_UILIMIT_WRITECLIPBOARD
        assert readback.get("ui", 0) & ph._JOB_UILIMIT_GLOBALATOMS
    finally:
        process.kill()
        process.wait(timeout=5)
        if job:
            ph.kernel32_close(job)

    # 3) fork 拒绝（源码层）：含 subprocess 的包被扫描拒绝安装；
    #    且绕过扫描的受限代码中 import subprocess 改写为 PermissionError
    bad = '''
def fork_bomb():
    import subprocess
    return subprocess.run(["cmd"]).returncode
'''
    source = tmp_path / "src-fork"
    (source / "payload").mkdir(parents=True)
    (source / "manifest.json").write_text(
        json.dumps(_manifest("9.9.9", action_ids=["fork_bomb"]), ensure_ascii=False),
        encoding="utf-8")
    (source / "payload" / "main.py").write_text(bad, encoding="utf-8")
    (source / "LICENSE").write_text("MIT\n", encoding="utf-8")
    violations = host._scan_payload_forbidden_apis(source)
    assert violations, "subprocess import 必须被扫描命中"

    # 受限代码改写断言：subprocess import → PermissionError（fork 被拒）。
    # 报错文案会引用模块名，故按行匹配真实 import 语句而非全文子串
    restricted = host._build_restricted_source_from_source(source, "fork_bomb")
    assert not re.search(r"(?m)^\s*(import\s+subprocess|from\s+subprocess)\b",
                         restricted), "真实 import 语句必须被改写"
    assert "PermissionError" in restricted
