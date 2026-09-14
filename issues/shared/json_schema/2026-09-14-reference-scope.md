# SCHEMA-001：Schema 迁移中的引用作用域不一致

日期：2026-09-14。优先级：P2。状态：已修复。来源：S-01 实施中的独立代码审查；问题只存在于本轮未提交版本，未认定为已发布故障。
范围：共享 Schema 预检与工具未知字段策略。

## 现象与根因

Schema 不仅是字典，也依赖引用资源作用域。初版迁移误将字典遍历或校验器复用等同于保持引用语义：

- 手写 JSON Pointer 跳过中间 `$id`，空 `$id` 又被误当新资源，导致合法本地引用被拒绝；命名锚点未预检。
- 同一 Python 子 Schema 在两个资源中复用时，只按节点 identity 去重，漏检第二个作用域的缺失引用。
- 工具用 `evolve(schema=effective)` 收紧未知字段，却保留旧 resolver；`child: {"$ref":"#"}` 仍指向原始未收紧根，放行嵌套额外字段。

## 方案与实施

按 [python-jsonschema 引用文档](https://python-jsonschema.readthedocs.io/en/stable/referencing/) 使用官方 referencing 的资源/解析器接口，不自行维护 Pointer 与 `$id` 算法。
先检查全部版本声明，再按节点及实际资源根 identity 去重检查引用；工具为 effective Schema 重新创建校验器，避免复用旧根。
共享字典、嵌入资源、空标识、缺失锚点与递归额外字段均先建立失败测试，再修复。

## 验证与经验

`test_json_schema.py` 的资源作用域、缺失锚点及复用字典用例分别先失败；工具递归未知字段用例先失败。
修复后六组定向测试 341 项通过，包括真实 ToolService 零执行测试。全量证据见 [S-01/S-02](../../../docs/history/completed-work.md#s-01-schema-dialect)。
变更 Schema 的根或包装策略时必须核对引用作用域，不能把字典相等或 validator 类型相同视作语义相同。
正式契约见[共享说明](../../../docs/shared_doc/json_schema.md)，迁移取舍见 [ADR-004](../../../adr/2026-09-14-json-schema-dialect.md)。
