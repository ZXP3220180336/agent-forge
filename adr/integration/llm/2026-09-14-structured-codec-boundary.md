# LLM-ADR-017：结构化输出的数据转换与编排分离

日期：2026-09-14。决定状态：已接受（用户授权执行 R1）。实现状态：已实现、已验证。
范围：`structured.py`、`structured_codec.py` 及对应测试、说明。关联：[R1 计划](../../../docs/todo.md#refactoring-plan)。

## 背景与备选

结构化组件同时承担 schema 副本、JSON 校验、错误文本、模型调用、预算与日志。
维护良率 RCA 的结构化证据报告时，纯数据规则与运行控制混合，难以局部确认输入是否被修改。

保留单文件虽少一次模块依赖，却不能隔离数据规则的变化；新增 Codec 类或通用阶段执行器没有独立状态需要。
采用内部函数模块，按 E9 的独立变化原因与可测试策略分离职责，不以文件行数为目标。

工业级参照：计划阶段已核查 Fowler 的 [Extract Function](https://refactoring.com/catalog/extractFunction.html)
与 [Extract Class](https://refactoring.com/catalog/extractClass.html)。本次选择函数提取，不引入新框架或状态类。

## 决定

- `structured_codec.py` 只依赖标准库和 jsonschema，负责 schema 副本、请求格式、解析/校验、反馈错误、
  日志摘要及回喂消息构造；不执行日志、调用或结算。
- `structured.py` 保留模型调用、三级降级、回喂/截断预算、usage、取消/期限及日志。
  codec 的校验器异常由原模块的观测包装记录并转为既有失败出口，不向公开调用者增加异常类型。
- 前两级使用 Draft7Validator，fallback 使用 jsonschema.validate；不统一 draft 选择或 schema 校验规则。
  本地 schema 与 strict 请求 schema 各自深拷贝，保持显式扩展许可的原有差异。
- LLMService 仍为唯一公开 Facade；内部纯函数测试调整为直接导入 codec，不保留旧私有函数兼容别名。

## 后果与边界

数据转换可以独立阅读，编排只在日志/异常边界承接校验失败；代价是增加一个内部依赖文件。
日志包装保留在编排模块有实际观测职责，不为缩行将它们继续分散。
重试数量、调用顺序、最后回喂成果解析和 usage 接管保持原样；R2～R6 不随本次实施。
出现新的真实数据规则时再评估提取边界，不据此建设通用 codec 框架。

## 实现与验证

- 既有结构化测试基线 60 项通过；先补两项公开调用行为测试，再提取 codec，定向 62 项通过。
- 新覆盖：嵌套 schema 的 strict/本地双副本与原输入不变；多错误完整回喂、日志摘要及消息不变。
- 独立审查未发现可证实行为漂移；全量 1046 项通过，文档对齐与差异检查通过，实际命令和环境修正记录见 R1 计划。
- 内部协作契约见 [结构化输出说明](../../../docs/integration_doc/llm_doc/structure.md#内部职责与协作边界)，
  当前代码、文档和测试映射见 [ALIGNMENT](../../../docs/ALIGNMENT.md)。
