# SCHEMA-002：预检失败契约未覆盖元校验的递归深度

日期：2026-09-15。优先级：P3。状态：已修复。来源：S-01 Schema 方言统一改动的独立代码审查（探针复现）。
范围：`app/shared/json_schema.py` 预检出口及其四个消费方。

## 现象与根因

`create_schema_validator` 对外声明「定义非法、版本不支持或本地引用不可解析」抛 `SchemaError`，四个消费方（`structured.py`、`validator.py`、`ToolRegistry.register`、`BaseTool` 导出的调用方）也都只捕获 `SchemaError`。

但预检中的元校验 `Draft202012Validator.check_schema` 是递归下降：极深嵌套会先耗尽递归深度，抛 `RecursionError` 而不是给出 `SchemaError`。探针实测默认递归上限下约 100 层触顶（50 层正常）。

`RecursionError` 因而越过所有消费方的捕获点逃逸到运行终态：structured 冲出降级链、validator 冲出 executor、ReAct 冲出 run、注册期冲出 `register`。

根因是「预检遍历有界」这一说法只覆盖自研的迭代遍历（`_check_schema_declarations` 与引用解析都用显式栈 + 身份去重，实测根 `#`、`$defs` 互引、`$anchor`、`$dynamicRef` 均正常终止），而真正递归的是库的元校验。

## 方案与实施

在 `_check_schema_declarations` 内收编：

```python
try:
    Draft202012Validator.check_schema(schema)
except RecursionError as error:
    raise SchemaError("Schema 嵌套过深，无法预检") from error
```

不自行加深度计数：递归发生在库内，调用方计数无法阻止其触顶。收编后对外失败契约仍然只有 `SchemaError`，消费方无需各自增加捕获。同步更新 `create_schema_validator` 的 `Raises` 与[共享契约](../../../docs/shared_doc/json_schema.md)。

## 验证与经验

探针：50 层正常；100 层由 `RecursionError` 变为 `SchemaError: Schema 嵌套过深，无法预检`。

`test_json_schema.py` 新增深嵌套用例（400 层，远高于实测阈值，使断言只依赖「耗尽递归深度」这一事实）。全量 1166 passed。

可达性说明：产品内 Schema 为 2–5 层，且 `output_schema` 只来自内部常量或调用方注入，不来自 API 请求，故本项属边界加固而非现网缺口。

可复用教训：声明「遍历有界」时要区分自研遍历与所依赖的库调用——两者的边界条件不同。对外失败契约应覆盖实际可达的异常类型，否则加了预检反而多出一条未收编的逃逸路径。

正式契约见[本地 Schema 契约](../../../docs/shared_doc/json_schema.md)，相关引用作用域问题见 [SCHEMA-001](2026-09-14-reference-scope.md)。
