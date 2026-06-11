# Agent v4 技术文档

## 1. 项目概述

Agent v4 是一个基于 Anthropic SDK 构建的 Python 编码智能体，使用 DeepSeek 作为 LLM 后端。v4 在 v3 基础上新增了**上下文压缩管线**、**Hook 生命周期系统**、**统一日志**和**子 Agent 派发**能力。

### 1.1 技术栈

| 组件 | 技术 |
|---|---|
| LLM SDK | `anthropic` (Python) |
| 后端模型 | DeepSeek v4 Flash (通过 Anthropic 兼容 API) |
| 配置管理 | `python-dotenv` + `.env` |
| Python 版本 | 3.11+ |

### 1.2 快速启动

```bash
# 1. 安装依赖
pip install anthropic python-dotenv

# 2. 配置 .env（见下方配置说明）

# 3. 运行
python main.py
```

---

## 2. 文件结构

```
v4/
├── .env                  # 环境变量配置（API Key、模型、参数）
├── .gitignore
├── config.py             # 统一配置中心，从 .env 加载所有配置
├── llm.py                # Anthropic 客户端单例
├── log.py                # 统一日志模块（log_event）
├── hook.py               # Hook 生命周期系统（权限、日志、控制流）
├── tools.py              # 工具定义与 handler（bash/read/write/edit/glob）
├── skill.py              # 技能加载与注册（从 skill/ 目录扫描 SKILL.md）
├── subagent.py           # 子 Agent 模块（任务派发，不支持嵌套）
├── compact.py            # 上下文压缩管线（4 级压缩策略）
├── main.py               # 入口，Agent 主循环
└── skill/                # 技能定义目录
    ├── code_review/SKILL.md
    ├── data_analysis/SKILL.md
    ├── debug_helper/SKILL.md
    └── translate/SKILL.md
```

### 2.1 模块依赖关系

```
main.py  （入口，Agent 主循环）
  ├── config.py    （统一配置中心）
  ├── llm.py       （Anthropic 客户端单例）
  ├── log.py       （统一日志，无依赖）
  ├── hook.py      （Hook 系统 ← config, log）
  ├── skill.py     （技能加载 ← config, log）
  ├── tools.py     （工具函数 ← config）
  ├── subagent.py  （子 Agent ← config, llm, hook, log）
  └── compact.py   （压缩管线 ← log, llm）
```

循环依赖处理：`subagent.py` 通过延迟导入 `from tools import TOOL_HANDLERS` 避免 `tools → subagent → tools` 循环。

---

## 3. 配置说明

所有配置通过 `.env` 文件管理，由 `config.py` 统一加载。

| 变量名 | 必填 | 默认值 | 说明 |
|---|---|---|---|
| `ANTHROPIC_BASE_URL` | 否 | (官方 API) | API 端点，DeepSeek 使用 `https://api.deepseek.com/anthropic` |
| `ANTHROPIC_AUTH_TOKEN` | 是 | — | API Key |
| `MODEL_ID` | 是 | — | 模型 ID，如 `deepseek-v4-flash` |
| `MAX_TOKENS` | 否 | `8000` | 主 Agent 单次最大输出 token |
| `SUB_MAX_TOKENS` | 否 | `4000` | 子 Agent 单次最大输出 token |
| `SUB_MAX_TURNS` | 否 | `30` | 子 Agent 最大循环轮次 |
| `PROMPT_NAME` | 否 | `s03` | REPL 提示符显示名称 |
| `SKILLS_DIR` | 否 | `./skill` | 技能目录路径 |

---

## 4. 核心架构

### 4.1 Agent 主循环 (`agent_loop`)

```
用户输入
  │
  ▼
┌──────────────────────────────────────────────────┐
│  agent_loop(messages)                            │
│  ┌────────────────────────────────────────────┐  │
│  │ 1. 上下文压缩预处理（0 API 调用）           │  │
│  │    tool_result_budget → snip → micro        │  │
│  │ 2. LLM 摘要压缩（条件触发，1 API 调用）     │  │
│  │ 3. 调用 LLM API                            │  │
│  │ 4. 判断 stop_reason                         │  │
│  │    ├─ tool_use → 执行工具 → 回到 1          │  │
│  │    └─ end_turn → 触发 Stop hook → 返回      │  │
│  └────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────┘
  │
  ▼
输出结果
```

**关键设计：**
- 循环直到 LLM 不再请求工具调用才退出
- 每轮迭代自动执行上下文压缩，防止 token 超限
- `Stop` hook 可以强制继续循环（返回值注入为 user message）

### 4.2 工具调用流程

```
LLM 返回 tool_use block
  │
  ▼
trigger_hook("PreToolUse", block)
  ├─ 自动派发 OnToolStart → 日志记录
  ├─ permission_hook → 权限检查（可阻断）
  │
  ├─ 被阻断 → 返回错误信息作为 tool_result
  │
  └─ 通过 → 执行 handler(block.input)
       │
       ▼
     trigger_hook("PostToolUse", block, output)
       ├─ 自动派发 OnToolEnd → 日志记录（含耗时）
       └─ large_output_hook → 大输出警告
```

### 4.3 工具列表

| 工具名 | Handler | 功能 |
|---|---|---|
| `bash` | `run_bash()` | 执行 shell 命令（120s 超时） |
| `read_file` | `run_read()` | 读取文件内容，支持行数限制 |
| `write_file` | `run_write()` | 写入文件（自动创建父目录） |
| `edit_file` | `run_edit()` | 精确文本替换（单次） |
| `glob` | `run_glob()` | 按模式搜索文件 |
| `compact` | `run_compact()` | 手动触发上下文压缩 |
| `spawn_subagent` | `spawn_subagent()` | 派发子 Agent 执行子任务 |

---

## 5. Hook 生命周期系统

Hook 系统是 v4 的核心扩展机制，所有权限检查、日志记录、行为控制都通过 Hook 实现。

### 5.1 Hook 类型

| Hook 名称 | 触发时机 | 返回值语义 |
|---|---|---|
| `UserPromptSubmit` | 用户输入进入 LLM 前 | 忽略（纯观察） |
| `PreToolUse` | 工具执行前 | 非 None → **阻断工具**，返回值成为 tool_result |
| `PostToolUse` | 工具执行后 | 忽略（纯观察） |
| `Stop` | Agent 主循环即将退出 | 非 None → **强制继续**，返回值注入为 user message |
| `OnToolStart` | 工具执行前（自动派发） | 忽略（日志专用） |
| `OnToolEnd` | 工具执行后（自动派发） | 忽略（日志专用） |

### 5.2 已注册的 Hook

| 函数 | Hook 点 | 职责 |
|---|---|---|
| `permission_hook` | `PreToolUse` | 阻断危险 bash 命令（deny list）；交互确认破坏性命令；阻止工作区外写入 |
| `large_output_hook` | `PostToolUse` | 输出超过 100K 字符时警告 |
| `context_inject_hook` | `UserPromptSubmit` | 记录工作目录 |
| `summary_hook` | `Stop` | 打印会话工具调用总数 |
| `tool_start_hook` | `OnToolStart` | 自动记录工具名称、ID、参数预览 |
| `tool_end_hook` | `OnToolEnd` | 自动记录工具耗时、输出大小 |

### 5.3 自动派发机制

`trigger_hook()` 函数内部自动处理 `OnToolStart` / `OnToolEnd` 的派发：

```python
def trigger_hook(hook_name, *args, **kwargs):
    # PreToolUse → 自动派发 OnToolStart，记录开始时间
    # 执行控制流 hook handlers
    # PostToolUse → 自动派发 OnToolEnd，计算耗时
```

业务代码只需调用 `trigger_hook("PreToolUse", block)` 和 `trigger_hook("PostToolUse", block, output)`，日志自动输出。

### 5.4 扩展 Hook

```python
# 在 hook.py 中定义函数
def my_custom_hook(block):
    """PreToolUse: 自定义逻辑"""
    if block.name == "bash" and "git push" in block.input.get("command", ""):
        log_event("CUSTOM", "git_push", cmd=block.input["command"])
    return None

# 注册
register_hook("PreToolUse", my_custom_hook)
```

---

## 6. 统一日志系统

### 6.1 设计原则

- **单一输出点**：所有日志通过 `log_event()` 函数输出，底层唯一调用 `print()`
- **Hook 自动触发**：工具调用的 start/end 日志由 `trigger_hook()` 内部自动派发
- **业务代码零侵入**：工具函数（`tools.py`）纯业务逻辑，不含任何日志语句

### 6.2 `log_event()` 接口

```python
log_event(category: str, event: str, **data)
```

**输出格式：** `[CATEGORY] event: key=value, key=value`

**示例：**
```
[TOOL] start: name=bash, id=toolu_01abc, args=['ls -la']
[TOOL] end: name=bash, elapsed=0.23s, output=1234 chars, rc=(ok)
[API] response: elapsed=2.15s, stop_reason=tool_use, text_blocks=1, tool_blocks=2, input_tokens=1500, output_tokens=300
[COMPACT] snip: before=30, after=20, head=10, tail=10
[SUBAGENT] spawn: task=Read and analyze config.py, model=deepseek-v4-flash, max_turns=30
[SESSION] round_start: round=1, input=帮我看看这个项目的结构
```

### 6.3 颜色方案

| 颜色 | ANSI 码 | 用途 |
|---|---|---|
| 灰色 | `\033[90m` | 普通信息（工具、API、压缩、迭代） |
| 黄色 | `\033[33m` | 警告（大输出、破坏性命令、应急压缩） |
| 红色 | `\033[31m` | 错误（权限阻断、超时、异常） |
| 洋红 | `\033[35m` | 子 Agent 事件 |
| 青色 | `\033[36m` | 用户交互（会话事件） |

### 6.4 日志事件分类

| Category | Event | 触发位置 | 说明 |
|---|---|---|---|
| `STARTUP` | `config` | main.py | 配置信息 |
| `STARTUP` | `llm` | main.py | LLM 客户端信息 |
| `STARTUP` | `tools` | main.py | 工具注册列表 |
| `STARTUP` | `system_prompt` | main.py | 系统提示词长度 |
| `ITERATION` | `start` | main.py | 迭代开始（轮次、消息数） |
| `ITERATION` | `end` | main.py | 迭代结束（工具结果数） |
| `API` | `response` | main.py | API 响应详情（耗时、token、blocks） |
| `TOOL` | `start` | hook.py (自动) | 工具开始执行 |
| `TOOL` | `end` | hook.py (自动) | 工具执行完成（含耗时） |
| `TOOL` | `warning` | hook.py | 大输出警告 |
| `TOOL_BLOCKED` | `blocked` | hook.py | 权限阻断 |
| `TOOL_BLOCKED` | `warning` | hook.py | 破坏性命令警告 |
| `COMPACT` | `budget` | compact.py | 大结果落盘 |
| `COMPACT` | `snip` | compact.py | 消息裁剪 |
| `COMPACT` | `micro` | compact.py | 旧结果压缩 |
| `COMPACT` | `llm_summary` | compact.py | LLM 摘要压缩 |
| `COMPACT` | `emergency` | compact.py | 应急压缩 |
| `SUBAGENT` | `spawn` | subagent.py | 子 Agent 创建 |
| `SUBAGENT` | `turn` | subagent.py | 子 Agent 轮次 |
| `SUBAGENT` | `api_response` | subagent.py | 子 Agent API 响应 |
| `SUBAGENT` | `finished` | subagent.py | 子 Agent 完成 |
| `SUBAGENT` | `done` | subagent.py | 子 Agent 总结（耗时、工具数） |
| `SKILL` | `scan_start` | skill.py | 技能扫描开始 |
| `SKILL` | `loaded` | skill.py | 技能加载成功 |
| `SKILL` | `skipped` | skill.py | 技能跳过（无 SKILL.md） |
| `SKILL` | `build_system` | skill.py | 系统提示词构建 |
| `SESSION` | `round_start` | main.py | 对话轮次开始 |
| `SESSION` | `prompt` | hook.py | 用户输入 |
| `SESSION` | `agent_finished` | main.py | Agent 准备结束 |
| `SESSION` | `force_continue` | main.py | Stop hook 强制继续 |
| `SESSION` | `stop` | hook.py | 会话结束（工具调用统计） |
| `SESSION` | `end` | main.py | 会话退出 |
| `ERROR` | `no_handler` | main.py | 工具无 handler |

---

## 7. 上下文压缩管线

当对话历史增长到接近 token 上限时，压缩管线自动运行，确保 API 调用不会因上下文过长而失败。

### 7.1 四级压缩策略

按顺序执行，每级都有独立的压缩逻辑：

```
Level 3 (L3) — tool_result_budget
  │  将超过 2048 字符的工具结果写入 ./tool_results/，消息中保留截断版本 + 文件路径
  │
  ▼
Level 1 (L1) — snip_compact
  │  消息数超过 20 时，保留前 10 + 后 10，丢弃中间
  │
  ▼
Level 2 (L2) — micro_compact
  │  工具结果超过 5 个时，将旧的替换为 "[工具结果被压缩]"，保留最新 5 个
  │
  ▼
Level 0 (LLM) — compact_history
     当序列化后的消息总长度超过 CONTEXT_LIMIT (50000 字符) 时触发
     使用 1 次 LLM 调用生成对话摘要，替换整个历史
     完整记录写入 ./transcripts/
```

### 7.2 应急压缩 (`emergency_compact`)

当 API 调用失败（如上下文溢出）时的兜底策略：
- 使用 LLM 生成摘要
- 保留最近 5 条原始消息 + 摘要
- 确保下一次 API 调用能成功

### 7.3 压缩工具

用户或 LLM 可通过 `compact` 工具手动触发压缩：
```json
{"name": "compact", "input": {"focus": "optional focus area"}}
```

---

## 8. 子 Agent 系统

### 8.1 设计

子 Agent 是独立的 Agent 循环，用于执行主 Agent 分配的子任务：

- **独立系统提示**：简化版，明确禁止嵌套派发
- **受限工具集**：bash、read_file、write_file、edit_file、glob（不含 spawn_subagent）
- **独立限制**：`SUB_MAX_TURNS` (30)、`SUB_MAX_TOKENS` (4000)
- **共享 Hook**：PreToolUse / PostToolUse hooks 对子 Agent 同样生效

### 8.2 调用方式

主 Agent 通过 `spawn_subagent` 工具派发任务：
```json
{"name": "spawn_subagent", "input": {"description": "Read config.py and summarize its contents"}}
```

### 8.3 执行流程

```
主 Agent 调用 spawn_subagent(description)
  │
  ▼
创建子 Agent 消息列表 [user: description]
  │
  ▼
┌─────────────────────────────────────────┐
│ 子 Agent 循环（最多 SUB_MAX_TURNS 轮） │
│  1. 调用 LLM API                       │
│  2. stop_reason != tool_use → 结束      │
│  3. 执行工具调用（通过 hook 系统）       │
│  4. 回到 1                              │
└─────────────────────────────────────────┘
  │
  ▼
提取最终文本结果，返回给主 Agent
```

### 8.4 结果提取策略

1. 从最后一条消息提取文本
2. 如果为空，从最后一条 assistant 消息提取
3. 如果仍然为空，返回默认错误信息

---

## 9. 技能系统

### 9.1 技能定义

每个技能是 `skill/` 目录下的子文件夹，包含 `SKILL.md` 文件：

```
skill/
└── code_review/
    └── SKILL.md    # YAML frontmatter + 技能正文
```

SKILL.md 格式：
```markdown
---
name: code_review
description: 对代码进行多维度审查
---

# 技能正文（Markdown 格式）
...
```

### 9.2 已有技能

| 技能 | 功能 |
|---|---|
| `code_review` | 多维度代码审查（结构、风格、缺陷、安全、性能） |
| `data_analysis` | 数据分析与可视化建议（支持 CSV/JSON/Excel） |
| `debug_helper` | 调试辅助（错误分析、根因定位、修复建议） |
| `translate` | 智能翻译（上下文感知、术语表支持） |

### 9.3 技能加载流程

1. `_scan_skills()` 扫描 `SKILLS_DIR` 下所有子目录
2. 解析每个 `SKILL.md` 的 YAML frontmatter（name、description）
3. 注册到全局 `SKILL_REGISTRY` 字典
4. `build_system()` 将技能目录注入系统提示词

---

## 10. 安全机制

### 10.1 命令过滤 (`permission_hook`)

**Deny List（直接阻断）：**
`rm -rf /`、`sudo`、`shutdown`、`reboot`、`mkfs`、`dd if=`

**Destructive List（交互确认）：**
`rm `、`> /etc/`、`chmod 777`

### 10.2 路径安全

`safe_path()` 函数确保所有文件操作都在工作区内：
```python
path = (WORKDIR / p).resolve()
if not path.is_relative_to(WORKDIR):
    raise ValueError(f"Path escapes workspace: {p}")
```

### 10.3 工作区外写入检查

`write_file` 和 `edit_file` 工具在写入前检查路径是否在工作区内，超出时需要用户交互确认。

---

## 11. 典型运行示例

```
输入问题，回车发送。输入 q 退出。

[STARTUP] config: workdir=D:\project, model=deepseek-v4-flash, max_tokens=8000
[STARTUP] llm: base_url=https://api.deepseek.com/anthropic
[STARTUP] tools: registered=['bash', 'read_file', 'write_file', 'edit_file', 'glob', 'compact', 'spawn_subagent']
[STARTUP] system_prompt: chars=456
[SKILL] scan_start: dir=D:\project\skill
[SKILL] loaded: name=code_review, chars=1200
[SKILL] loaded: name=data_analysis, chars=980
[SKILL] scan_done: total=4

s03 >> 帮我看看 main.py 的代码结构

[SESSION] round_start: round=1, input=帮我看看 main.py 的代码结构
[SESSION] prompt: workdir=D:\project
[ITERATION] start: iteration=1, messages=1
[API] response: elapsed=1.85s, stop_reason=tool_use, text_blocks=1, tool_blocks=1, input_tokens=1200, output_tokens=150
[TOOL] start: name=read_file, id=toolu_01abc, args=['main.py', None]
[TOOL] end: name=read_file, elapsed=0.01s, output=2340 chars, rc=
[ITERATION] end: iteration=1, tool_results=1
[ITERATION] start: iteration=2, messages=3
[API] response: elapsed=2.31s, stop_reason=end_turn, text_blocks=1, tool_blocks=0, input_tokens=3500, output_tokens=400
[SESSION] agent_finished: stop_reason=end_turn
[SESSION] stop: tool_calls=1

（Agent 的文本回复输出到这里）
```