# Agent 迭代总览

从 v1 到 v4 的四次迭代，逐步构建一个基于 Anthropic SDK 的 Python 编码智能体。

---

## 版本对比

| 维度 | v1 | v2 | v3 | v4 |
|---|---|---|---|---|
| **文件数** | 3 | 5 | 7 | 9 |
| **Hook 系统** | — | 4 种事件 | 4 种事件（修复返回值） | 6 种事件 + 自动派发 |
| **权限系统** | 独立模块 | Hook 包装 | Hook 包装 | Hook 包装 |
| **技能系统** | — | SKILL.md | SKILL.md | SKILL.md |
| **子 Agent** | — | — | ✓ | ✓ |
| **上下文压缩** | — | — | — | 4 级管线 |
| **集中式配置** | — | — | ✓ | ✓ |
| **统一日志** | — | — | — | ✓ (log_event + hook 自动触发) |
| **LLM 客户端** | 内联 | 内联 | 独立模块 | 独立模块 |

---

## 架构演进

### v1 — 最简实现

```
main.py ←→ tools.py ←→ permisssion.py
```

三个文件，直接调用，无中间层。验证 Anthropic Messages API + tool_use 基本可用性。

### v2 — 引入 Hook 与技能

```
main.py ←→ hook.py ←→ permisssion.py（遗留）
   ↕           ↕
tools.py    skill.py → skill/*.md
```

Agent 循环重构为纯编排层，权限/日志/监控通过 Hook 解耦。技能系统通过 Markdown + YAML frontmatter 扩展系统提示词。

### v3 — 模块化 + 子 Agent

```
main.py
  ├── config.py    （集中配置）
  ├── llm.py       （LLM 单例）
  ├── hook.py      （Hook 系统）
  ├── tools.py     （工具函数）
  ├── skill.py     （技能加载）
  └── subagent.py  （子 Agent）
```

配置集中化，LLM 客户端独立，新增子 Agent 任务派发。修复 v2 的 `trigger_hook` 不返回值问题。

### v4 — 统一日志 + 上下文压缩

```
main.py
  ├── config.py
  ├── llm.py
  ├── log.py       （统一日志 ← 新增）
  ├── hook.py      （扩展：OnToolStart/OnToolEnd 自动派发）
  ├── tools.py     （纯业务逻辑，无日志语句）
  ├── skill.py
  ├── subagent.py
  └── compact.py   （4 级压缩管线 ← 新增）
```

所有日志通过 `log_event()` 统一输出，工具调用日志由 Hook 自动触发。上下文压缩管线（L3 大结果落盘 → L1 消息裁剪 → L2 旧结果压缩 → LLM 摘要）防止 token 超限。

---

## 核心能力清单

### 工具（全版本通用）

| 工具 | 功能 | 引入版本 |
|---|---|---|
| `bash` | 执行 shell 命令（120s 超时） | v1 |
| `read_file` | 读取文件内容 | v1 |
| `write_file` | 写入文件 | v1 |
| `edit_file` | 精确文本替换 | v1 |
| `glob` | 文件模式搜索 | v1 |
| `spawn_subagent` | 派发子 Agent | v3 |
| `compact` | 手动触发上下文压缩 | v4 |

### Hook 生命周期

| Hook | 触发时机 | 引入版本 |
|---|---|---|
| `UserPromptSubmit` | 用户输入前 | v2 |
| `PreToolUse` | 工具执行前（可阻断） | v2 |
| `PostToolUse` | 工具执行后 | v2 |
| `Stop` | Agent 即将退出（可强制继续） | v2 |
| `OnToolStart` | 工具执行前（自动派发，日志专用） | v4 |
| `OnToolEnd` | 工具执行后（自动派发，日志专用） | v4 |

### 技能（全版本通用，v2 引入）

| 技能 | 功能 |
|---|---|
| `code_review` | 多维度代码审查（结构、风格、缺陷、安全、性能） |
| `data_analysis` | 数据分析与可视化建议 |
| `debug_helper` | 调试辅助（错误分析、根因定位、修复建议） |
| `translate` | 智能翻译（上下文感知、术语表支持） |

---

## 各版本文档

- [v1/README.md](v1/README.md) — 最简实现
- [v2/README.md](v2/README.md) — Hook + 技能系统
- [v3/README.md](v3/README.md) — 模块化 + 子 Agent
- [v4/README.md](v4/README.md) — 统一日志 + 上下文压缩
