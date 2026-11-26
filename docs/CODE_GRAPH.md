# 只读代码解析与可追溯依赖图

状态：TASK-032 的解析器核心。它先作为仓库内只读分析器交付；TASK-023 的签名包格式已合入，
TASK-024 的受限宿主仍负责把它装进插件边界。当前没有执行任何目标项目代码。

## 能力

`src/code_graph` 用 Python `ast` 解析目标目录：

- 节点：
  - `module`：每个 `.py` 文件，带相对路径、模块名、SHA-256；
  - `function`、`class`：函数、异步函数、类及其嵌套符号，ID 包含文件与限定名；
  - `external`：无法在仓库内解析的外部模块。
- 边：
  - `contains`：模块 → 符号；
  - `imports`：模块 → 本地模块、被导入符号或 external 节点；
  - `calls`：可静态解析的函数/方法 → 目标符号或 external 节点。
- 诊断：
  - `syntax_error`：语法错误文件仍保留 module 节点，但跳过符号解析；
  - `dynamic_call`：无法静态解析的调用，例如 `obj.method()`；
  - `invalid_relative_import`：越界的相对导入；
  - `module_name_collision`：同一模块名映射到多个文件。

同名符号通过 `symbol:<相对路径>:<限定名>` 区分；例如两个文件里的 `process` 不会合并。

## 只读保证

- 只调用 `Path.read_bytes()` 和 `ast.parse()`；不 import、不执行、不写目标项目。
- 不解析模型、不访问网络、不启动目标项目的子进程。
- 测试用会在模块顶层写 marker 的 fixture 验证：生成图后 marker 不存在。
- 扫描跳过符号链接和 Windows 目录联接点。指定 `--out` 时，输出文件必须位于扫描目录之外。

## 增量刷新

`CodeGraphSession` 保存每个 `.py` 文件的 SHA-256：

```python
session = CodeGraphSession(root)
graph = session.build()
change = session.refresh()
change.changed_files   # 变更/新增/删除的文件
change.added_edges
change.removed_edges
change.graph           # 重建后的图
```

单文件变更时重建 AST 图并返回边差异；旧边不会残留。文件被删除时，对应 module/symbol 和边
一起消失。

## CLI

```powershell
& ./.venv/Scripts/python.exe scripts/code_graph.py `
  --root tests `
  --out (Join-Path $env:TEMP 'task032-graph.json')
```

输出 `mokoa.code-graph/v1` JSON，包含 `nodes`、`edges`、`diagnostics` 和 `files` 哈希；
`--refresh` 会再调用一次刷新并附上 `changed_files`、`added_edges`、`removed_edges`。

本机对 `tests/` 的运行结果：1207 个节点、4558 条边、2045 条 `dynamic_call` 诊断，0 个语法错误。
这不是对全仓语义质量的结论，只是解析器夹具和规模证据。

## 解析边界

- 这是静态语法解析，不做类型推断、跨函数数据流或第三方包源码分析。
- `obj.method()`、反射、`getattr`、`__import__` 等调用只会产生动态调用诊断，不猜测目标。
- 外部模块保留为 `external:<name>`，不下载、不执行。
- 本任务只交解析器核心；TASK-023 的签名插件包已合入但本解析器尚未打包，TASK-024 的宿主尚未集成。
- 没有实现 TASK-033 的源码视图跳转或 AI 解释联动。
- 图形/节点可读性、前端可视化和真机验收不在本任务范围。

## 测试覆盖

`tests/test_code_graph.py` 覆盖：

- 循环导入 `pkg/a.py ↔ pkg/b.py`；
- 跨文件导入后调用解析；
- 同名函数符号；
- 外部模块 `json` 与调用边；
- 语法错误文件；
- 动态调用诊断；
- 顶层副作用 fixture 不执行；
- 单文件变更后旧导入/调用边失效、新边出现；
- 无变化刷新为空 diff。
