# OmniAble 插件包格式与版本契约

状态：TASK-023 / Issue #31 的格式与验包契约。本文和 `src/plugin_package` 只定义
**离线包、签名、信任和校验**。桌面宿主提供已签名包的安装、启用状态和审计。
`GET /agent/plugins` 的 `executable` 表示当前宿主是否已注册可执行动作。
桌面宿主在 TASK-024 的文件、网络和子进程隔离通过验收前保持插件代码执行关闭。

## 包布局

插件包是扩展名为 `.mko` 的确定性 ZIP：

```text
manifest.json          # 受签名保护的清单
signature.json         # Ed25519 签名和 manifest 哈希
payload/...            # 插件文件；验包器只读取字节，不导入、不执行
LICENSE                # 可选，manifest.license_file 指向它
```

不允许绝对路径、`..`、盘符、反斜杠、符号链接、重复 ZIP 路径或未列入清单的额外文件。
包大小、单文件大小、文件数和解压总大小都有上限。
Windows 保留名称、路径尾随点或空格、文件与目录冲突及仅大小写不同的路径均拒绝。
打包源中的链接和重解析点也会被拒绝。验签和整包摘要使用同一次读取的包字节。

## manifest 示例

```json
{
  "schema": "mokoa.plugin/v1",
  "id": "com.example.echo",
  "name": "Echo",
  "version": "1.0.0",
  "host_api": ">=1.0.0 <2.0.0",
  "entry": "payload/main.py",
  "license": "MIT",
  "license_file": "LICENSE",
  "publisher": {
    "key_id": "example-root-2026",
    "name": "Example Publisher"
  },
  "capabilities": ["files.read"],
  "actions": [
    {
      "id": "echo",
      "name": "Echo text",
      "description": "Return supplied text.",
      "parameters": {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
        "additionalProperties": false
      },
      "capabilities": ["files.read"],
      "mutates": false
    }
  ],
  "dependencies": [],
  "files": {
    "LICENSE": "sha256:...",
    "payload/main.py": "sha256:..."
  }
}
```

### 字段约定

| 字段 | 说明 |
|---|---|
| `schema` | 固定 `mokoa.plugin/v1`；改变不兼容语义必须升级 schema |
| `id` | 稳定的小写反向域名式 ID；同一本地目录不能重复 |
| `version` | 插件版本，SemVer 2.0 |
| `host_api` | 宿主 API 范围，SemVer 比较子句，例如 `>=1.0.0 <2.0.0` |
| `entry` | 包内相对入口，必须在 `files` 中 |
| `license` / `license_file` | SPDX 标识或名称，以及可选的包内许可证文件 |
| `publisher.key_id` | 签发者 ID；必须与 `signature.json.key_id` 和本地信任根匹配 |
| `capabilities` | 包级能力声明；校验器只接受允许列表 |
| `actions` | 动作目录；`id` 唯一、稳定，`parameters` 是 JSON Schema 对象 |
| `dependencies` | 依赖锁；每个依赖有 ID、精确 SemVer 和可选包摘要 |
| `files` | 所有非控制文件的 `sha256:<hex>`；由 pack 生成并签名 |

允许的能力：

```text
files.read files.write network.local network.internet process.spawn
desktop.uia clipboard.read clipboard.write memory.read memory.write
```

动作级 `capabilities` 必须是包级能力的子集。`mutates` 表示该动作可能改变外部状态；
最终授权、确认和工作区绑定仍由宿主执行。

## 版本规则

- 插件版本和宿主版本使用 SemVer 2.0。
- `host_api` 是空格或逗号分隔的比较子句合取：
  `>=1.0.0 <2.0.0`、`>=1.2.0,<1.5.0`。
- 校验时必须提供 `--host-version`；范围不满足时返回 `incompatible_host_api`。
- 依赖版本是精确锁定版本；有 `digest` 时还必须匹配依赖包文件的 SHA-256。
- 包 schema 是另一条版本轴；`v1` 验包器不接受未知 schema。

## 签名与本地信任

`signature.json`：

```json
{
  "schema": "mokoa.plugin-signature/v1",
  "algorithm": "ed25519",
  "key_id": "example-root-2026",
  "manifest_sha256": "sha256:<canonical manifest bytes>",
  "signature": "<base64 Ed25519 signature>"
}
```

- 签名对象是 `manifest.json` 的规范 UTF-8 JSON：键排序、无多余空白、`ensure_ascii=false`。
- `manifest_sha256` 绑定实际的 manifest 字节；签名绑定规范 manifest。这样空白改动也会被发现。
- 本地信任根 `plugins/trust/test-root.json` 保存 Ed25519 公钥；校验器不做网络请求。
- 本地撤销清单 `plugins/trust/revocations.json` 按 `key_id` 拒绝已撤销签发者。
- 信任根和撤销清单都是本地文件，更新它们不等于运行插件。
- 示例信任根是 **测试 fixture**，不是生产信任根；不要把它用于真实插件分发。

## 校验顺序

1. 读取 ZIP，检查路径、重复项、符号链接、大小和文件数。
2. 读取并验证 `manifest.json` 的字段、动作/依赖唯一性和文件摘要。
3. 验证 `signature.json` 的 schema、key ID、manifest 哈希和 Ed25519 签名。
4. 检查 `host_api` 与 `--host-version`。
5. 检查包内文件集合与 manifest 完全一致，并逐个核对摘要。
6. 有 `--catalog` 时检查依赖存在、版本和可选摘要，并检测重复插件 ID。

**任何阶段都不 import、exec、eval 或把 payload 解压到可执行目录。** payload 只是待宿主导入的数据；
宿主的权限、沙箱和插件执行不在验包器范围内。

## 错误码

| code | 触发 |
|---|---|
| `unsafe_path` | 绝对路径、`..`、盘符、反斜杠、符号链接或越界路径 |
| `duplicate_path` | ZIP 重复路径、manifest 重复文件、源目录重复文件 |
| `duplicate_action` / `duplicate_dependency` | 动作或依赖 ID 重复 |
| `duplicate_plugin_id` / `duplicate_plugin_name` | 同一 catalog 目录出现重复插件 ID 或显示名称 |
| `missing_file` / `extra_file` | manifest 文件集合与 ZIP 不一致 |
| `file_digest_mismatch` | payload 被篡改 |
| `manifest_digest_mismatch` / `invalid_signature` | manifest 或签名被篡改 |
| `unknown_signer` / `revoked_signer` | 签发者不在本地信任根 / 已被撤销 |
| `missing_dependency` / `dependency_mismatch` | 依赖缺失、版本或摘要不匹配 |
| `incompatible_host_api` | 宿主版本不在 `host_api` 范围内 |
| `invalid_schema` / `invalid_manifest` / `invalid_version` | 清单或版本格式错误 |
| `package_too_large` / `file_too_large` / `too_many_files` | 超过离线包限制 |

CLI 以 JSON 输出 `code`、`message` 和可定位的 `path`，失败返回非零退出码。

## 命令

```powershell
# 验示例包（不联网、不执行 payload）
./.venv/Scripts/python.exe scripts/plugin_package.py verify `
  plugins/samples/echo.mko `
  --trust plugins/trust/test-root.json `
  --revocations plugins/trust/revocations.json `
  --host-version 1.4.0

# 验目录并检查重复 ID / 依赖
./.venv/Scripts/python.exe scripts/plugin_package.py verify-catalog `
  plugins/samples --trust plugins/trust/test-root.json `
  --revocations plugins/trust/revocations.json --host-version 1.4.0
```

打包需要发布者自己的 Ed25519 私钥；私钥不进入包，也不提交到仓库：

```powershell
./.venv/Scripts/python.exe scripts/plugin_package.py pack `
  plugins/samples/echo --key C:\path\to\publisher-key.pem --out echo.mko
```

测试使用临时生成的密钥对；仓库只提交示例包和测试公钥信任根。

## 与后续任务

- TASK-024 才能决定插件的安装、启用/停用、工作进程、文件/网络/进程能力限制和动作注册。
- TASK-021 的动作目录字段应与本文件 `actions` 的 `id`、参数、`mutates` 和 `capabilities` 对齐。
- TASK-032/033 的代码解析插件只读运行在宿主边界内；本格式不授予运行时访问。
- 本契约不声明生产签名密钥、真实发布通道、远程安装或在线撤销服务。
