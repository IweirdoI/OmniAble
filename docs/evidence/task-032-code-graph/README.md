# TASK-032 只读代码解析验收

日期：2026-11-26

## 交付

- `src/code_graph/graph.py`：AST 只读解析、模块/符号/外部节点、`contains`/`imports`/`calls` 边、语法/动态调用诊断、文件哈希和增量刷新。
- `scripts/code_graph.py`：输出 `mokoa.code-graph/v1` JSON。
- `tests/test_code_graph.py`：循环导入、同名符号、外部模块、语法错误、动态调用、不执行目标代码、单文件刷新。
- `docs/CODE_GRAPH.md`：格式、解析边界和命令。

## 实测

```text
pytest tests/test_code_graph.py -q
4 passed
```

全量 Python 回归：`982 passed, 6 skipped`（本机 Windows env；包含已合入的插件包与离线守卫测试）。

CLI 对 `tests/`：

```text
python scripts/code_graph.py --root tests --out (Join-Path $env:TEMP 'task032-graph.json')
{"ok": true, "nodes": 1207, "edges": 4558}
```

图统计：

```text
schema=mokoa.code-graph/v1
nodes=1207
edges=4558
edge_kinds=contains, imports, calls
node_kinds=module, class, function, external
diagnostics=2045
diagnostic_codes=dynamic_call
```

测试 fixture 还实际验证了：

- `pkg/a.py ↔ pkg/b.py` 循环导入边；
- `Process.run -> helper` 跨文件调用边；
- `same_a.process` 与 `same_b.process` 为两个不同符号 ID；
- `broken.py` 语法错误记录而不崩溃；
- `obj.method()` 产生 `dynamic_call`；
- 顶层写 marker 的 fixture 未执行；
- 修改 `pkg/a.py` 后旧 `a -> b` 导入边和旧调用边被移除，新边出现；
- 无变化刷新为空 diff。

## 边界

- 当前是 Windows 本地解析器核心；TASK-023 的签名包格式已合入，但本解析器尚未打包，也未接入 TASK-024 宿主。
- 没有做 TASK-033 的源码跳转、AI 解释联动或前端视图。
- 不执行目标项目代码，不处理第三方包源码、类型推断或运行时动态调用。
- 没有网络、模型、真实用户数据或构建产物进入仓库。
- 1207/4558 只是本机 `tests/` 规模数据，不代表全仓性能或语义质量。
