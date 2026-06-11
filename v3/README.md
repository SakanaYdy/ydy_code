# Agent v3 技术文档

## 1. 项目概述

Agent v3 在 v2 基础上新增了**集中式配置**、**独立 LLM 客户端模块**、**子 Agent 系统**，并修复了 v2 的关键缺陷（`trigger_hook` 不返回值导致权限 hook 形同虚设）。

### 1.1 技术栈

| 组件 | 技术 |
|---|---|
| LLM SDK | `anthropic` (Python) |
| 后端模型 | DeepSeek v4 Flash (通过 Anthropic 兼容 API) |
| 配置管理 | `python-dotenv` + `.env` + 集中式 `config.py` |
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
v3/
├── .env                  # 环境变量配置
├── .gitignore
├── config.py             # 统一配置中心
├── llm.py                # Anthropic 客户端单例
├── main.py               # 入口 + Agent 主循环 + REPL
├── hook.py               # Hook 系统（生命周期 + 权限）
├── tools.py              # 工具定义 + handler
├── skill.py              # 技能加载与注册
├── subagent.py           # 子 Agent 模块
└── skill/
    ├── code_review/SKILL.md
    ├── data_analysis/SKILL.md
    ├── debug_helper/SKILL.md
    └── translate/SKILL.md
```

### 2.1 模块依赖关系

```
main.py  （入口）
  ├── config.py    （统一配置）
  ├── llm.py       （LLM 客户端）
  ├── hook.py      （Hook 系统 ← config）
  ├── skill.py     （技能加载 ← config）
  ├── tools.py     （工具函数 ← config, llm）
  └── subagent.py  （子 Agent ← config, llm, hook）
```

---

## 3. 核心架构

### 3.1 Agent 主循环

```
用户输入
  │
  ▼
trigger_hook("UserPromptSubmit", query)
  │
  ▼
agent_loop(messages)
  ├─ 调用 LLM API
  ├─ 追加 assistant 响应
  │
  ├─ stop_reason != tool_use？
  │   └─ trigger_hook("Stop", messages)
  │       └─ 返回值非空 → 注入为 user message，继续循环
  │
  └─ 有 tool_use block？
      ├─ trigger_hook("PreToolUse", block)
      │   └─ 返回值非空 → 阻断，返回值成为 tool_result
      ├─ 执行 handler(block.input)
      └─ trigger_hook("PostToolUse", block, output)
```

### 3.2 Hook 系统

四种生命周期事件，与 v2 相同但 `trigger_hook` 修复为返回值：

| Hook | 触发时机 | 返回值语义 |
|---|---|---|
| `UserPromptSubmit` | 用户输入进入 LLM 前 | 忽略 |
| `PreToolUse` | 工具执行前 | 非 None → **阻断工具** |
| `PostToolUse` | 工具执行后 | 忽略 |
| `Stop` | Agent 即将退出 | 非 None → **强制继续** |

**已注册的内置 Hook：**

| 函数 | Hook 点 | 职责 |
|---|---|---|
| `permission_hook` | `PreToolUse` | deny list 硬阻断 + 破坏性命令交互确认 + 工作区外写入检查 |
| `log_hook` | `PreToolUse` | 记录工具名称 + 参数预览 |
| `large_output_hook` | `PostToolUse` | 输出超过 100K 字符时警告 |
| `context_inject_hook` | `UserPromptSubmit` | 记录工作目录 |
| `summary_hook` | `Stop` | 打印工具调用总数 |

### 3.3 工具列表

| 工具 | Handler | 功能 |
|---|---|---|
| `bash` | `run_bash()` | 执行 shell 命令（120s 超时） |
| `read_file` | `run_read()` | 读取文件，支持行数限制 |
| `write_file` | `run_write()` | 写入文件（自动创建父目录） |
| `edit_file` | `run_edit()` | 精确文本替换（单次） |
| `glob` | `run_glob()` | 按模式搜索文件 |
| `spawn_subagent` | `spawn_subagent()` | 派发子 Agent 执行子任务 |

### 3.4 子 Agent 系统

v3 新增，主 Agent 可将子任务委派给独立的子 Agent：

```
主 Agent 调用 spawn_subagent(description)
  │
  ▼
子 Agent 独立循环（最多 SUB_MAX_TURNS 轮）
  ├─ 独立系统提示（禁止嵌套派发）
  ├─ 受限工具集（不含 spawn_subagent）
  ├─ 独立 token 限制（SUB_MAX_TOKENS）
  └─ 共享 PreToolUse hook（权限检查生效）
  │
  ▼
提取最终文本结果，返回给主 Agent
```

**设计要点：**
- 子 Agent 不能嵌套派发（工具列表排除 `spawn_subagent`）
- 子 Agent 通过延迟导入 `from tools import TOOL_HANDLERS` 避免循环依赖
- 结果提取策略：最后消息 → 最后 assistant 消息 → 默认错误信息

### 3.5 技能系统

与 v2 相同，基于 SKILL.md + YAML frontmatter：

**已有技能：**
- `code_review` — 多维度代码审查（结构、风格、缺陷、安全、性能）
- `data_analysis` — 数据分析与可视化建议
- `debug_helper` — 调试辅助（错误分析、根因定位、修复建议）
- `translate` — 智能翻译（上下文感知、术语表支持）

---

## 4. 配置

v3 引入集中式配置模块 `config.py`，所有模块从这里导入：

| 变量 | 默认值 | 说明 |
|---|---|---|
| `ANTHROPIC_BASE_URL` | (官方 API) | `.env` 配置 |
| `ANTHROPIC_AUTH_TOKEN` | — | `.env` 配置 |
| `MODEL_ID` | — | `.env` 配置 |
| `MAX_TOKENS` | `8000` | 主 Agent 单次最大输出 |
| `SUB_MAX_TOKENS` | `4000` | 子 Agent 单次最大输出 |
| `SUB_MAX_TURNS` | `30` | 子 Agent 最大轮次 |
| `PROMPT_NAME` | `s03` | REPL 提示符 |
| `SKILLS_DIR` | `WORKDIR/skill` | 技能目录 |

---

## 5. v2 → v3 变化

| 维度 | v2 | v3 |
|---|---|---|
| 配置 | 分散在各模块，硬编码路径 | 集中式 `config.py`，环境变量可配 |
| LLM 客户端 | 定义在 `tools.py` | 独立 `llm.py` 模块 |
| Hook 返回值 | `trigger_hook` 不返回值，权限 hook 无效 | 修复：返回第一个非 None 结果，权限 hook 生效 |
| 子 Agent | 无 | `subagent.py` + `spawn_subagent` 工具 |
| 技能目录 | 硬编码绝对路径 | 环境变量可配，默认 `WORKDIR/skill` |
| 技能扫描 bug | `and` 逻辑错误 | 修复为 `or` |
| 函数命名 | `list_skils`（拼写错误） | `list_skills`（修正） |
| 文件数 | 5 个 | 7 个（+config.py, +llm.py, +subagent.py） |
