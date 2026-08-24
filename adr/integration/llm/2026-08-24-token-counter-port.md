# TokenCounter 端口：tiktoken 隔离到集成层（依赖倒置 + 单一事实源）

> **状态**：✅ 已采纳
> **决策日期**：2026-08-24
> **涉及模块**：`app/domain/ports/token_counter.py` · `app/integration/llm/token_counter.py` · `app/application/context/context_manager.py` · `app/integration/llm/llm_service.py`
> **关联文档**：[token_counter.md](../../../docs/integration_doc/llm_doc/token_counter.md) · [context.md](../../../docs/application_doc/context_doc/context.md) · [architecture.md](../../../docs/architecture.md)

---

## Context

- 架构「零外部框架依赖层」硬约束：tiktoken 计数经 TokenCounter 端口在集成层实现，领域/应用层不得直接依赖 tiktoken。
- 现状违反：`context_manager.py` 模块顶层 `import tiktoken`、构造时解析 encoder——应用层硬依赖 tiktoken。
- 连带问题：encoder 解析逻辑在 `context_manager.py` 与 `llm_service._get_encoder` 各写一遍（重复）；`ContextManager.count_messages_tokens` 对 content=None 未防御（LLM 模块已用 `_content_to_text` 修复同型缺陷，ContextManager 仍暴露）。

## Decision

**落地 `TokenCounter` 端口：`app/domain/ports/token_counter.py` 定义协议，`app/integration/llm/token_counter.py` 唯一实现（tiktoken 唯一使用点），`ContextManager` 构造注入。**

1. **端口契约**：`@runtime_checkable Protocol`，`count_tokens(text)` / `count_messages_tokens(messages)` 两个方法。
2. **集成实现**：`TiktokenTokenCounter(model)` 构造时经 `get_encoder` 解析编码器（进程缓存 + 未知模型回退 cl100k_base）；`count_messages_tokens` 内建 `content_to_text` 防御（None / 多模态 list 不崩）。
3. **单一事实源**：`get_encoder` / `content_to_text` 提炼为本模块模块级函数，`llm_service._count_prompt_tokens` 以别名 import 复用——消除解析逻辑重复；别名即模块属性，`test_llm_service.py` 的 monkeypatch 字符串路径 `llm_service._get_encoder` 保持有效，测试零断裂。
4. **LLM 模块不迁移到端口**：集成层允许直接使用 tiktoken；TPM 限流口径（+max_tokens 输出余量）是集成层内部细节，不入端口（YAGNI）。
5. **装配**：`container.initialize` 构造 `TiktokenTokenCounter(model=settings.llm_model_id)` 注入 `ContextManager`（去掉原 `model_name` 参数，encoder 职责移交）。
6. **工业级参照**：Clean Architecture Port/Adapter（依赖倒置）；LiteLLM / LangChain 的 token 计数封装均将第三方 tokenizer 隔离在集成层。

## Consequences

- **正面**：应用层不再依赖 tiktoken（架构约束合规）；tiktoken 解析单一事实源；`ContextManager` 潜在 content=None TypeError 顺带修复；`ContextManager` 可注入 fake counter 测试（端口可替换）。
- **负面**：ContextManager 构造签名变更（`model_name` → `token_counter`），波及 3 个测试文件构造点（已适配）；新增一个端口 + 一个实现模块（ALIGNMENT 登记）。
