# TOOLS-037 http_api 校验失败回显完整参数，headers 凭据泄露

> **状态**：✅ 已修复（2026-08-20）
> **优先级**：P3（安全，次要项）
> **来源**：2026-08-18 工具模块代码审核（external 组 · 次要项 9）
> **涉及模块**：`app/integration/tools/external/http_api.py`（HttpApiTool.execute）
> **关联文档**：[external.md](../../../docs/integration_doc/tools_doc/external.md)

---

## 问题描述

### 现象

`http_api` 校验失败错误含 `参数有误: {kwargs!s}`（TOOLS-031 后为 `_invalid_params_result`），把 headers（可能含 `Authorization`）明文回显进 error / 审计日志。

### 影响

凭据经错误信息 / 审计落盘泄露。

### 根因

校验失败错误复用通用格式（回显全量 kwargs），未对 headers 脱敏。

---

## 修复方案

`http_api` 校验失败改脱敏错误（不回显 kwargs）：

```python
return ToolResult(success=False, content="",
    error="参数有误：url 必填、method 限 GET/POST/PUT/DELETE、headers/body 为 JSON")
```

**取舍**：http_api 特殊（headers 可含凭据），不用通用 `_invalid_params_result`（回显 kwargs）——错误信息给归因提示但不含参数明文。

---

## 实施记录

| 文件 | 改动 | 回归测试 |
| --- | --- | --- |
| `app/integration/tools/external/http_api.py` | 校验失败改脱敏错误（不回显 headers） | `tests/unit/test_http_api_tool.py` 新增 `test_validation_error_does_not_leak_headers`（凭据不进 error） |

---

## 验证

- 相关测试 **13 passed**（http_api）
- `scripts/verify_alignment.py`：ALIGNMENT 校验通过

---

## 教训沉淀

- **错误信息也是泄露面**：`kwargs!s` 回显会把凭据带进 error / 审计——含敏感字段的工具校验失败须脱敏归因（给提示不含明文）。
- **通用工厂的例外**：`_invalid_params_result` 通用格式对普通工具 OK，但 headers 敏感的工具要特化脱敏。
