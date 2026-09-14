# SCHEMA-003：指针解析异常穿透预检失败契约

日期：2026-09-15。优先级：P2。状态：已修复。来源：Schema 方言统一改动的独立复审（探针复现）。
范围：`app/shared/json_schema.py` 引用解析；症状波及结构化输出、ReAct、工具注册与参数校验四个消费方。

## 现象与根因

`create_schema_validator` 的引用解析只捕获 `Unresolvable`：

```python
try:
    resolved = source_resolver.lookup(reference)
except Unresolvable as error:
    raise SchemaError(f"无法解析本地 Schema 引用：{reference}") from error
```

`referencing` 的指针穿越（`_core.pointer` 的 `contents[segment]`）未防护全部内容形态，实测：

| 定义 | 底层异常 |
| --- | --- |
| `{"x": 1, "$ref": "#/x/y"}` | `TypeError: 'int' object is not subscriptable` |
| `{"a": [1], "$ref": "#/a/zz"}` | `ValueError: invalid literal for int() with base 10: 'zz'` |
| `{"x": 1, "$dynamicRef": "#/x/y"}` | `TypeError`（同形） |

越界下标（`#/a/5`）与缺失键（`#/y`）已由库转成 `Unresolvable`，因此不在本项范围。

异常穿透预检后，structured 入口的 `except SchemaError` 不成立，不再「记 ERROR 并返回 None」，而是冲出降级链；其余三个消费方同样只捕 `SchemaError`。预检的对外承诺（失败只有一种类型）因此在真实输入下不成立。

## 方案与实施

```python
# referencing 的指针穿越未防护全部内容形态：穿标量抛 TypeError，
# 数组遇非数字下标抛 ValueError（越界下标与缺失键已由其转为 Unresolvable）。
# 预检对外只暴露 SchemaError，这些一并连同异常链收编。
except (Unresolvable, TypeError, ValueError) as error:
    raise SchemaError(f"无法解析本地 Schema 引用：{reference}") from error
```

不扩大到 `except Exception`：那会掩盖库内真实缺陷。原始异常保留在 `__cause__`，排障信息不丢。

## 验证与经验

探针：三类形态均转为 `SchemaError`，`__cause__` 分别为 `TypeError` / `ValueError`。

`test_json_schema.py` 新增 3 个参数化用例（穿标量 / 数组非数字下标 / `$dynamicRef` 同形），修复前均为红。全量 1166 passed，`verify_alignment` 与 `git diff --check` 通过。

可复用教训：依赖库的异常翻译不是全覆盖的。对外承诺单一失败类型时，要按实际可达的输入形态逐条验证，而不是只接住库的主异常——否则预检反而多出一条未收编的逃逸路径。

正式契约见[本地 Schema 契约](../../../docs/shared_doc/json_schema.md)，相关失败契约问题见 [SCHEMA-002](2026-09-15-deep-nesting-recursion.md)。
