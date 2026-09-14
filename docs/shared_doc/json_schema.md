# 本地 JSON Schema 契约

对应代码：`app/shared/json_schema.py`。更新：2026-09-14。实现与验证映射见 [ALIGNMENT](../ALIGNMENT.md)。

## 职责与接口

`create_schema_validator(schema)` 为结构化输出、ReAct 最终答案和工具参数创建固定的 `Draft202012Validator`。
只负责定义预检与校验器构建；不做模型调用、日志、错误回喂、类型转换或额外字段策略。

| 输入或场景 | 行为 |
| --- | --- |
| 无 `$schema` | 按 Draft 2020-12 解释 |
| 显式版本 | 只接受 `https://json-schema.org/draft/2020-12/schema` 及其空 fragment（末尾 `#`）形式 |
| 旧版本、未知版本、非法定义、嵌套超出预检深度 | 抛 `jsonschema.SchemaError`；不会自动降级到其他版本 |
| 内嵌子 Schema | 同样检查声明；不把 `const`、`enum`、`default`、`examples` 中的实例数据或业务字段名当声明 |
| `$ref` / `$dynamicRef` | 支持以 `#` 开头的文档内指针和锚点，按 `$id` 资源作用域解析；缺失目标、指针穿越非容器内容或数组非法下标、非 fragment 引用均抛 SchemaError |
| 布尔 Schema | 共享函数支持；各业务入口仍遵守其原有 dict 类型契约 |
| `format` / 默认值 | 不启用 format 断言，不填充 default；未知扩展关键字仍按规范处理，预检不是拼写检查器 |

根定义使用一次 `check_schema` 递归覆盖标准子 Schema；声明扫描记录本次调用内已覆盖的对象，标准引用目标不重复元校验或声明扫描。仅对扩展位置尚未覆盖的目标补元校验并扫描其标准子树；先发现子目标再发现扩展父目标时，父目标仍需补验。覆盖集合不跨调用保留，引用访问仍按节点与资源根分别去重。子 Schema 定位与引用解析使用 `referencing` 的 2020-12 规范及公开 Resolver API。
先扫描全部内嵌声明，再解析引用目标，防止子校验器隐式切换版本。引用去重同时考虑节点与资源根，保证递归引用预检有界。
`check_schema` 本身是递归下降，极深嵌套会先耗尽递归深度而非给出 SchemaError；指针解析在穿越非容器内容或数组非数字下标时也会抛底层 `TypeError` / `ValueError`。两者都在预检内统一收编为 SchemaError（原始异常保留在 `__cause__`）——对外失败契约只有 SchemaError，各消费方不必分别捕获。
显式空 Registry 禁止隐式网络或文件获取。若将来需要外部 Schema 注册、跨文档引用或多版本支持，须独立设计接入和版本契约。

工厂不修改输入、不缓存可变 Schema。调用方追加约束后应重新创建校验器；`evolve(schema=...)` 可能保留旧 resolver，导致递归 `#` 使用旧定义。

## 消费边界

| 调用方 | 定义错误 | 实例错误及额外字段 |
| --- | --- | --- |
| [结构化输出](../integration_doc/llm_doc/structure.md) | 调用前记 ERROR 并返回 None，零模型调用 | 前两级全部错误回喂，fallback 保留 best_match 代表错误；保留本地/strict 请求的双副本与原额外字段策略 |
| [ReAct](../domain_doc/reasoning_doc/react.md) | 注入 final_answer 前抛 SchemaError，零模型调用 | best_match 代表错误进入原协议修正；不追加额外字段约束 |
| [工具注册](../integration_doc/tools_doc/registry.md) | 注册期抛 SchemaError，工具不入容器（内置逐工具兜底、外部按文件回滚） | 不适用：注册期只做定义预检，实例校验仍在执行链 |
| [工具校验](../integration_doc/tools_doc/validator.md) | 转 ValidationIssue；工具导出时直接抛 SchemaError | 全量中文问题，保留 reject_unknown；非法参数零工具执行 |

统一版本不表示不同入口的额外字段策略相同，也不表示模型供应商支持完整 2020-12。模型请求继续受对应接口子集约束，本地校验保留最终约束责任。

## 验证与依据

`tests/unit/test_json_schema.py` 覆盖版本、四种关键字语义、嵌套声明、普通数据不误判、指针/锚点/资源作用域、指针穿越非法内容形态、深嵌套失败契约与输入不变。
业务测试见 `test_generate_structured.py`、`test_react_strategy.py`、`test_tool_validator.py`，观察三级结果、调用次数、usage 和执行阻断。
决策与取舍见 [ADR-004](../../adr/2026-09-14-json-schema-dialect.md)。
