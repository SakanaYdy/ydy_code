# Agent v1 技术文档

## 1. 项目概述

Agent v1 是编码智能体的最简实现，包含三个文件，无 Hook 机制、无技能系统、无上下文压缩。目标是验证 Anthropic Messages API + tool_use 的基本可用性。

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
v1/
├── .env              # 环境变量配置
├── main.py           # 入口 + Agent 主循环 + REPL
├── tools.py          # 工具定义 + handler + LLM 客户端
└── permisssion.py    # 三关权限管线（注意文件名拼写）
```

仅 3 个文件，无子目录。

---

## 3. 核心架构

### 3.1 Agent 主循环

```
用户输入 → agent_loop(messages)
  │
  ├─ 调用 LLM API（messages + tools）
  ├─ 追加 assistant 响应到 messages
  │
  ├─ 有 tool_use block？
  │   ├─ 是 → 权限检查 → 执行 handler → 收集结果 → 追加为 user message → 循环
  │   └─ 否 → 提取 text block → 返回结果
```

- 无限循环，直到 LLM 不再请求工具调用
- 无上下文压缩，history 列表无限增长
- 无 Stop hook，直接返回

### 3.2 工具列表

| 工具 | Handler | 功能 |
|---|---|---|
| `bash` | `run_bash()` | 执行 shell 命令（120s 超时，50K 字符截断） |
| `read_file` | `run_read()` | 读取文件，支持行数限制 |
| `write_file` | `run_write()` | 写入文件（自动创建父目录） |
| `edit_file` | `run_edit()` | 精确文本替换（单次） |
| `glob` | `run_glob()` | 按模式搜索文件 |

所有文件操作通过 `safe_path()` 沙箱化，防止路径逃逸。

### 3.3 权限系统 (`permisssion.py`)

三关管线，在工具执行前调用：

```
Gate 1 — 硬拒绝列表
  │  rm -rf /, sudo, shutdown, reboot, mkfs, dd if=, > /dev/sda
  │  命中即阻断，无需用户确认
  │
  ▼
Gate 2 — 规则匹配
  │  write/edit: 路径逃逸工作区
  │  bash: rm, > /etc/, chmod 777
  │  命中则进入 Gate 3
  │
  ▼
Gate 3 — 用户确认
  交互式 [y/N] 提示，默认拒绝
```

### 3.4 LLM 调用

- 客户端：`anthropic.Anthropic()`，通过 `.env` 配置端点和密钥
- 调用：`client.messages.create(model, system, messages, tools, max_tokens=8000)`
- 同步阻塞，无流式输出，无重试逻辑

---

## 4. 配置

通过 `.env` 管理：

| 变量 | 说明 |
|---|---|
| `ANTHROPIC_BASE_URL` | API 端点 |
| `ANTHROPIC_AUTH_TOKEN` | API Key |
| `MODEL_ID` | 模型 ID |

其余参数（`max_tokens`、超时时间、拒绝列表）均为代码内硬编码。

---

## 5. v1 的特点与局限

| 特点 | 说明 |
|---|---|
| 架构 | 最简三文件结构，职责分离清晰 |
| 权限 | 独立的三关权限管线 |
| 工具 | 5 个基础工具（bash + 4 个文件操作） |
| 局限 | 无 Hook、无技能、无压缩、无子 Agent、配置硬编码 |
