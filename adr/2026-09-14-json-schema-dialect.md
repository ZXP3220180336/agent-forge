# ADR-004：本地 JSON Schema 统一 Draft 2020-12

日期：2026-09-14。决策：已接受。实现：已验证。范围：shared、LLM、ReAct、工具参数。

## 背景与工业级参照

R1 已通过独立提交 `d2a5f44` 提取 codec，保留前两级 Draft7 与 fallback 自动选择的历史行为。
同一 Schema 使用 dependentRequired、prefixItems、$ref 同级约束或 unevaluatedProperties 时，不同路径会产生不同校验结果。
工具已有 2020-12 决策，因此统一向该版本迁移，服务于计划、证据和报告约束的一致性。

备选：维持混用会继续漏检；统一 Draft7 会倒退工具契约；自动按声明多版本选择增加兼容维护成本，暂无真实消费需求。
采用显式 2020-12，三个真实调用方共享纯函数，不引入类、配置开关或领域到 integration 的依赖。

工业级参照：

- [python-jsonschema 校验 API](https://python-jsonschema.readthedocs.io/en/stable/validate/)：显式 validator、check_schema、iter_errors；未提供 cls 的 validate 按声明或库默认选版本。
- [官方引用配置](https://python-jsonschema.readthedocs.io/en/stable/referencing/)：通过 Registry 和固定 Specification 管理资源，引用解析交给 referencing，不手写 JSON Pointer 或资源作用域算法。
- [2020-12 发布说明](https://json-schema.org/draft/2020-12/release-notes)：位置数组改用 prefixItems，动态引用与评估语义不能视作旧版的完全兼容扩展。

## 决定

正式契约只维护于[共享 Schema 说明](../docs/shared_doc/json_schema.md)。缺省按 2020-12；显式其他版本拒绝，包括子 Schema。
引用仅支持内嵌资源与 fragment，不启用网络获取。内置 Schema 不批量添加 `$schema`，避免无意义改变模型请求载荷。
模型供应商的 Schema 子集不在本轮转换，也不宣称获得完整规范支持。

定义错误与实例错误分离：structured 保留 None 和 ERROR 出口，但提前到模型调用前；ReAct 注入前抛 SchemaError；工具校验转换为问题列表，工具导出抛 SchemaError。工具侧的定义预检执行点收敛在唯一注册入口（`ToolRegistry.register`），使内置装配的逐工具兜底与外部加载的文件级回滚各自生效，不把单点定义错误放大成整批工具失效。
实例错误保持全量回喂与 best_match 代表错误的不同呈现，原预算、usage、取消和额外字段策略不变。

`referencing` 原为 jsonschema 的锁定传递依赖，现因直接使用其公开 API 声明为直接依赖；离线更新锁文件，不升级包版本。
本决策替代 [LLM-ADR-017](integration/llm/2026-09-14-structured-codec-boundary.md) 中保留混合版本及仅依赖标准库/jsonschema 的条款；纯数据/编排边界继续有效。
工具既有 [2020-12 决策](integration/tools/2026-08-17-jsonschema-strict-validation.md) 延续；新增统一定义预检。

## 后果与边界

收益：路径不再决定 Schema 版本；非法定义不要求模型修复。代价：旧版本声明和外部引用需调用方迁移，预检增加本地计算；暂不缓存可变定义。
format 断言、多版本、远程注册、供应商子集转换均为独立升级项。Schema 本身递归可用于递归数据；本轮只保证预检遍历有界，不承诺对任意恶意递归 Schema 提供运行时计算配额。

## 实施与验证

首次三级行为测试 11 failed / 16 passed，证实新版约束漏检、位置数组误拒绝和非法定义仍调用模型；ReAct 预检测试 3 failed，工具侧新增用例 16 failed。
独立审查发现新实现的资源作用域误判、共享字典跨资源漏检及 evolve 沿用旧解析器问题，均补先失败测试再修复。
改用规范库引用解析，工具针对收紧后 Schema 重新创建校验器。
实施期复审另修三项：定义预检执行点移到注册入口（[TOOLS-051](../issues/integration/tools/2026-09-15-registry-schema-preflight.md)）；元校验的递归深度与指针穿越的底层 `TypeError` / `ValueError` 一并收编为 SchemaError，使失败契约只暴露一种异常（[SCHEMA-002](../issues/shared/json_schema/2026-09-15-deep-nesting-recursion.md)、[SCHEMA-003](../issues/shared/json_schema/2026-09-15-pointer-resolution-exception-leak.md)）；外部工具回滚按「已取得资源」而非「已注册」建名单，避免注册失败实例的资源无人释放（[TOOLS-052](../issues/integration/tools/2026-09-15-register-rollback-resource-leak.md)）。
最终回归：全量 1166 passed，`verify_alignment` 与 `git diff --check` 通过。
执行记录见 [S-01](../docs/todo.md)，当前映射见 [ALIGNMENT](../docs/ALIGNMENT.md)。
