# Agent v2 技术文档

## 1. 项目概述

Agent v2 在 v1 基础上引入了 **Hook 生命周期系统**和**技能系统**，将 Agent 主循环重构为纯编排层，所有横切关注点（权限、日志、监控）通过 Hook 解耦。

### 1.1 技术栈

| 组件 | 技术 |
|---|---|
| LLM SDK | `anthropic` (Python) |
| 后端模型 | DeepSeek v4 Flash (通过 Anthropic 兼容 API) |
| 配置管理 | `python-dotenv` + `.env` |
| Python 版本 | 3.11+ |

### 1.2 快速启动

```bash
pip install anthropic python-dotenv
# 配置 .env
python main.py
```

---

## 2. 文件结构

```
v2/
├── .env                  # 环境变量配置
├── main.py               # 入口 + Agent 主循环 + REPL
├── tools.py              # 工具定义 + handler + LLM 客户端
├── hook.py               # Hook 注册表 + 触发器 + 内置 hooks
├── skill.py              # 技能加载与注册
├── permisssion.py        # 独立权限管线（v1 遗留，未使用）
└── skill/                # 技能定义目录
    ├── code_review/SKILL.md
    ├── data_analysis/SKILL.md
    ├── debug_helper/SKILL.md
    └── translate/SKILL.md
```

---

## 3. 核心架构

### 3.1 Agent 主循环

```
用户输入
  │
  ▼
trigger_hook("UserPromptSubmit", query)    ← 日志记录工作目录
  │
  ▼
agent_loop(messages)
  ├─ 调用 LLM API
  ├─ 追加 assistant 响应
  │
  ├─ stop_reason != tool_use？
  │   └─ trigger_hook("Stop", messages)    ← 可强制继续
  │
  └─ 有 tool_use block？
      ├─ trigger_hook("PreToolUse", block) ← 权限检查 + 日志
      │   └─ 返回值非空 → 阻断工具
      ├─ 执行 handler
      └─ trigger_hook("PostToolUse")       ← 大输出警告
```

### 3.2 Hook 系统

v2 的核心新增，四种生命周期事件：

| Hook | 触发时机 | 返回值语义 |
|---|---|---|
| `UserPromptSubmit` | 用户输入进入 LLM 前 | 忽略（纯观察） |
| `PreToolUse` | 工具执行前 | 非 None → 阻断工具 |
| `PostToolUse` | 工具执行后 | 忽略（纯观察） |
| `Stop` | Agent 即将退出 | 非 None → 强制继续 |

**API：**
- `register_hook(name, func)` — 注册处理函数
- `trigger_hook(name, *args, **kwargs)` — 触发事件

**已注册的内置 Hook：**

| 函数 | Hook 点 | 职责 |
|---|---|---|
| `context_inject_hook` | `UserPromptSubmit` | 记录工作目录 |
| `permission_hook` | `PreToolUse` | 权限检查（deny list + 交互确认） |
| `log_hook` | `PreToolUse` | 记录每次工具调用 |
| `large_output_hook` | `PostToolUse` | 大输出警告（>100K 字符） |
| `summary_hook` | `Stop` | 打印工具调用总数 |

> **注意：** v2 的 `trigger_hook` 不返回 hook 处理函数的返回值，因此权限 hook 虽然存在但无法真正阻断工具执行。这是 v2 的已知缺陷，在 v3 中修复。

### 3.3 工具列表

| 工具 | Handler | 功能 |
|---|---|---|
| `bash` | `run_bash()` | 执行 shell 命令（120s 超时） |
| `read_file` | `run_read()` | 读取文件，支持行数限制 |
| `write_file` | `run_write()` | 写入文件（自动创建父目录） |
| `edit_file` | `run_edit()` | 精确文本替换（单次） |
| `glob` | `run_glob()` | 按模式搜索文件 |

### 3.4 技能系统

v2 新增，基于 Markdown + YAML frontmatter 的插件机制：

```
skill/
└── code_review/
    └── SKILL.md
```

SKILL.md 格式：
```markdown
---
name: code_review
description: 对代码进行多维度审查
---
# 技能正文（注入系统提示词）
```

**加载流程：**
1. `_scan_skills()` 扫描 `skill/` 目录下所有子目录
2. 解析 YAML frontmatter 提取 name、description
3. 注册到全局 `SKILL_REGISTRY`
4. `build_system()` 将技能目录注入系统提示词

**已有技能：**
- `code_review` — 多维度代码审查
- `data_analysis` — 数据分析与可视化建议
- `debug_helper` — 调试辅助
- `translate` — 智能翻译

---

## 4. 配置

通过 `.env` 管理（与 v1 相同）：

| 变量 | 说明 |
|---|---|
| `ANTHROPIC_BASE_URL` | API 端点 |
| `ANTHROPIC_AUTH_TOKEN` | API Key |
| `MODEL_ID` | 模型 ID |

`WORKDIR`、`max_tokens`、超时时间等仍为硬编码。`SKILLS_DIR` 在 `skill.py` 中硬编码为绝对路径。

---

## 5. v1 → v2 变化

| 维度 | v1 | v2 |
|---|---|---|
| Hook 系统 | 无 | 4 种生命周期事件 + 注册/触发 API |
| 权限处理 | 内联调用 `check_permission()` | 包装为 `PreToolUse` hook |
| 技能系统 | 无 | SKILL.md + frontmatter 解析 + 系统提示词注入 |
| 系统提示词 | 硬编码一行 | 动态生成（含技能目录） |
| Stop 行为 | 直接返回 | Stop hook 可强制继续 |
| 日志 | 无 | log_hook + large_output_hook + summary_hook |
| 工具 | 5 个 | 5 个（相同） |
| 配置 | 硬编码 + .env | 相同（未改善） |
