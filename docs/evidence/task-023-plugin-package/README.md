# TASK-023 插件包格式与离线验签验收

日期：2026-11-25

## 交付

- `src/plugin_package/`：manifest 校验、SemVer/宿主范围、Ed25519 签名、本地信任根/撤销清单、目录重复检查、打包与验包。
- `scripts/plugin_package.py`：`pack`、`verify`、`verify-catalog` CLI。
- `plugins/samples/echo.mko`：预构建示例包；`plugins/samples/echo/` 为源目录。
- `plugins/trust/test-root.json`、`plugins/trust/revocations.json`：本地测试信任根和空撤销清单。
- `docs/PLUGIN_PACKAGE.md`：包格式、版本、签名和错误码契约。

示例信任根是测试 fixture；签名私钥未提交，也不用于生产分发。

## 验证

```text
$ python scripts/plugin_package.py verify plugins/samples/echo.mko \
    --trust plugins/trust/test-root.json \
    --revocations plugins/trust/revocations.json \
    --host-version 1.4.0

{
  "ok": true,
  "id": "com.mokoa.sample.echo",
  "version": "1.0.0",
  "host_api": ">=1.0.0 <2.0.0",
  "key_id": "mokoa-test-root-2026",
  "publisher": "MOKOA Test Fixture",
  "files": ["LICENSE", "payload/main.py"],
  "package_sha256": "sha256:685bea447aa45cb71993c361a33949761b1f29c2163fd3455a1fcc2643cec0d1"
}
```

```text
pytest tests/test_plugin_package.py -q
16 passed
```

覆盖的错误场景：

- payload / manifest 篡改
- 未知签发者
- 已撤销签发者
- 动作重名、插件 ID 重名、插件显示名重名
- 重复 ZIP 路径
- 缺依赖、依赖版本/摘要锁定
- 宿主 API 不兼容
- 越界/盘符/符号链接路径
- 验包器不执行 payload（使用会写 marker 的 fixture 验证）

## 边界

- 当前是 Windows 本地离线验包；没有声明在干净机器、真实生产信任根或真实分发通道上验收。
- 没有实现 TASK-024 的插件宿主、权限执行、工作进程和动作注册。
- 没有真实用户资料、外部模型、网络请求或生产签名私钥进入包或仓库。
- 示例 `.mko` 是格式 fixture，不是生产插件。