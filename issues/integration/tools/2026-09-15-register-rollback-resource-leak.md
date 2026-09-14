# TOOLS-052 注册失败回滚遗漏当前实例的资源释放

日期：2026-09-15。优先级：P2。状态：已修复。来源：Schema 注册期预检改动的独立复审（探针复现）。
范围：`app/integration/tools/loader.py` 文件级回滚。

## 现象与根因

`_load_file` 的失败回滚遍历 `registered`，而该名单只包含**已成功进入注册中心**的实例：

```python
tool = cls()
await tool.on_load()          # 资源已建立
...
self._service.register(tool)  # 现在会因 Schema 预检失败而抛错
registered.append(tool)       # 失败时这一行没执行到
```

`register` 抛错后进入回滚分支，当前实例不在 `registered` 中，其 `on_unload()` 从不被调用；`_file_tools[path]` 也未登记，后续扫描不再追踪它。探针（插件把生命周期事件落盘）实测：失败后 trace 只有 `on_load;`，无 `on_unload;`。

根因是回滚名单的语义——它按「已进入目标状态（已注册）」建立，而清理需要的是「已取得资源（`on_load` 成功）」。同文件的重名分支已就地 `await tool.on_unload()`，说明该形态本就存在，只是 `register` 此前有重名前置检查、实际不会失败，缺口才不可达。本次把 Schema 预检放进 `register`（[TOOLS-051](2026-09-15-registry-schema-preflight.md)）后，这条路径变可达。

## 方案与实施

新增 `loaded` 名单（`on_load` 成功即入列），回滚遍历它；`registered` 保留给「无可用工具」判断与文件归属登记。

- 重名分支就地释放后 `pop()`，避免成功路径重复释放。
- 回滚对未注册实例调用 `unregister` 是 no-op，无需分支判断。

**边界（未改）**：`on_load` 自身抛异常不属于本项——该实例从未取得可确认的资源状态，保持既有语义（`test_on_load_failure_skips_tool` 钉住）。若要覆盖「部分取得也回滚」，需另立决定。

## 验证与经验

探针：修复后 trace 为 `on_load;on_unload;`，资源恰好释放一次。

`test_tool_loader.py` 新增用例：同一文件内放「合法工具 + Schema 非法的插件」，断言两者都不在注册表，且 trace 为 `load,load,unload,unload`（已注册实例与当前实例各释放一次）；修复前为红。全量 1166 passed，`verify_alignment` 与 `git diff --check` 通过。

可复用教训：回滚名单要按「已取得资源」建立，而不是按「已进入目标状态」。给某一步骤新增失败可能时，必须回头核对清理名单是否覆盖它——不可达的缺口会随新失败路径立即变可达。

正式契约见[外部工具热加载](../../../docs/integration_doc/tools_doc/external.md)与[注册中心说明](../../../docs/integration_doc/tools_doc/registry.md)。
