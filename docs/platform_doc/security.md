# 安全设计文档

> **更新日期**：2026-08-16
> **文档定位**：Agent 系统安全规范——威胁模型、数据安全、工具安全、密钥管理；
> 归拢现有零散安全实践（日志脱敏 / 拒答处理 / 代码执行黑名单）为统一规范
> **实现状态**：🔶 部分已实施（零散实践待归拢；威胁模型与安全规范待完善）

---

## 📋 目录

- [定位与目标](#定位与目标)
- [威胁模型](#威胁模型)
- [数据安全](#数据安全)
- [工具安全](#工具安全)
- [密钥管理](#密钥管理)
- [相关文档](#相关文档)

---

## 定位与目标

Agent 系统的安全**规范体系**——覆盖 Agent 特有的风险面（提示注入、工具越权）与通用
安全基线（敏感数据、密钥）。Yield RCA 场景承载良率 / 晶圆等**敏感工业数据**，泄露面
收敛是硬约束。

**目标**：

1. **威胁面明确**：Agent 特有威胁（prompt injection / 工具越权）建模并设防
2. **数据安全基线**：敏感数据分级、日志 / 响应泄露面收敛
3. **工具权限最小化**：代码执行 / 文件 / 网络访问的权限边界
4. **密钥合规**：密钥不入库、可轮换

## 威胁模型

> ⬜ 待完善。占位：提示注入（prompt injection，含间接注入）、工具参数注入、
> 工具越权（危险命令 / 越界文件）、敏感数据经 LLM 输出外泄、拒绝服务（配额耗尽）。

## 数据安全

> 🔶 部分已实施。现有实践：LLM 校验失败日志脱敏（[LLM-037](../../issues/integration/llm/2026-08-16-schema-validation-log-redaction.md)）、
> 拒答文本截断落盘（[LLM-008](../../issues/integration/llm/2026-08-16-refusal-log-truncation.md)）、
> 事件日志只记元数据（[logging.md](observability/logging.md)）。
> 待规划：Yield 数据分级、全链路泄露面收敛基线。

## 工具安全

> 🔶 部分已实施。现有实践：`code_exec` 危险命令黑名单（[builtin.md](../integration_doc/tools_doc/builtin_doc/builtin.md)）、
> 文件读写工具边界。待规划：沙箱隔离、网络访问控制、权限最小化基线。

## 密钥管理

> ✅ 已实施基线：配置从 `.env` 加载（[config.md](../config_doc/config.md)），禁止硬编码、
> 禁止提交密钥（CLAUDE.md 项目通用规则）。待规划：密钥轮换流程。

---

## 相关文档

- [architecture.md](../architecture.md)（分层与安全责任归属）
- [platform_doc/observability/logging.md](observability/logging.md)（日志脱敏 / 事件元数据）
- [integration_doc/tools_doc/builtin_doc/builtin.md](../integration_doc/tools_doc/builtin_doc/builtin.md)（内置工具安全边界）
- [config_doc/config.md](../config_doc/config.md)（密钥加载）
- 问题记录归档：[issues/integration/llm/](../../issues/integration/llm/README.md)
