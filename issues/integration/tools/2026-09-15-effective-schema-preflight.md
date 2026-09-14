# TOOLS-053：包装后 Schema 预检异常未转换

日期：2026-09-15。优先级：P2。状态：已修复。来源：用户 P3-1 优化的边界核查。
范围：`ParameterValidator.validate`。

## 现象与根因

原定义含 `additionalProperties.$defs.value`，并通过 `#/additionalProperties/$defs/value` 引用时原预检合法；严格未知字段策略将该字段覆盖为 `False`，导致引用失效。有效副本构建处于 `SchemaError` 捕获外，异常泄漏而非返回既有定义错误问题列表。

## 方案与实施

核对 [jsonschema API](https://python-jsonschema.readthedocs.io/en/stable/validate/) 的 `evolve` 保留解析行为和 [SCHEMA-001](../../shared/json_schema/2026-09-14-reference-scope.md) 后，保持修改后重新构建，并将原定义与有效副本的构建纳入同一异常转换边界。

附带 P3-1 优化只在 `reject_unknown=False` 或原 `additionalProperties` 已为 `False` 时复用原校验器。不能仅在覆盖已有字段时预检原定义：新注入字段也可能意外修复原来的悬空引用。其余情形保留双预检，不影响 executor、工具自身校验及导出入口的职责。

## 验证与经验

原实现的有效副本失效测试先复现 SchemaError 泄漏；修复后返回单个“Schema 定义无效”问题。独立复审对过宽优化补了普通及 URI 编码悬空引用的两项失败测试，再收窄优化条件；被覆盖子定义的非法关键字、旧方言及失效引用仍拒绝。直接消费入口定向 336 passed；最终全量结果见 [S-02](../../../docs/todo.md)。

P3-2 同步 `ToolService.register` 的 docstring 和组件方法表，明确 SchemaError；其注册行为未变化。

经验：Schema 包装可能使引用失效，也可能使原悬空引用变为可解析，不能只按关键字类型推断原预检是否可省。正式契约见[参数校验器](../../../docs/integration_doc/tools_doc/validator.md)。
