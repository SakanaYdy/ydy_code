# Agent 迭代总览

从 v1 到 v8 的八次迭代，逐步构建一个完整的 Claude Code 架构复现。每个版本解决一个明确的架构问题，层层递进。

---

## 版本对比

| 维度 | v1 | v2 | v3 | v4 | v5 | v6 | v7 | v8 |
|---|---|---|---|---|---|---|---|---|
| **文件数** | 3 | 5 | 7 | 9 | 11 | 12 | 13 | 15 |
| **Hook 系统** | — | 4 种事件 | 4 种事件 | 6 种事件 | 6 种事件 | 6 种事件 | 6 种事件 | 6 种事件 |
| **权限系统** | 独立模块 | Hook 包装 | Hook 包装 | Hook 包装 | Hook 包装 | Hook 包装 | Hook 包装 | Hook 包装 |
| **技能系统** | — | SKILL.md | SKILL.md | SKILL.md | SKILL.md | SKILL.md | SKILL.md | SKILL.md |
| **子 Agent** | — | — | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| **上下文压缩** | — | — | — | 4 级管线 | 4 级管线 | 4 级管线 | 4 级管线 | 4 级管线 |
| **统一日志** | — | — | — | ✓ | ✓ | ✓ | ✓ | ✓ |
| **持久记忆** | — | — | — | — | ✓ | ✓ | ✓ | ✓ |
| **错误恢复** | — | — | — | — | 3 条路径 | 3 条路径 | 3 条路径 | 3 条路径 |
| **任务系统** | — | — | — | — | — | DAG + 后台 | DAG + 后台 | DAG + 后台 + 自动认领 |
| **多Agent协作** | — | — | — | — | — | — | 消息总线 + 协议 | 消息总线 + 协议 + 自主 |
| **Worktree 隔离** | — | — | — | — | — | — | — | ✓ |
| **MCP 插件** | — | — | — | — | — | — | — | ✓ |
| **Lead 工具数** | 5 | 6 | 7 | 7 | 7 | 12 | 18 | 22 |

---

## 架构演进

### v1 — 最简实现

```
v1/
├── main.py          入口 · 一切逻辑的容器
├── tools.py         工具实现 + 工具定义 + LLM客户端(重复)
└── permisssion.py   权限管道(3级检查)
```

三个文件，直接调用，无中间层。验证 Anthropic Messages API + tool_use 基本可用性。

### v2 — 引入 Hook 与技能

```
v2/
├── main.py          入口 · agent_loop(纯编排)
├── tools.py         5个工具实现
├── hook.py          Hook注册表 + 触发逻辑 + 5个handler ← 新增
├── skill.py         技能扫描 + SKILL.md解析 ← 新增
└── permisssion.py   遗留(已被hook.py替代)
```

Agent 循环重构为纯编排层，权限/日志/监控通过 Hook 解耦。技能系统通过 Markdown + YAML frontmatter 扩展系统提示词。

### v3 — 模块化 + 子 Agent

```
v3/
├── config.py        统一配置中心 ← 新增
├── llm.py           Anthropic客户端单例 ← 新增
├── main.py          入口 · agent_loop + 工具注册
├── tools.py         5个工具(从config/llm导入)
├── hook.py          Hook系统(4点+5handler)
├── skill.py         技能加载
└── subagent.py      子Agent派发 ← 新增
```

配置集中化，LLM 客户端独立，新增子 Agent 任务派发。修复 v2 的 `trigger_hook` 不返回值问题。

### v4 — 统一日志 + 上下文压缩

```
v4/
├── config.py        统一配置中心
├── llm.py           LLM客户端单例
├── log.py           统一日志模块 ← 新增
├── main.py          入口 · agent_loop + 压缩 + 日志
├── tools.py         5个工具(纯业务逻辑, 无日志语句)
├── hook.py          扩展: OnToolStart/OnToolEnd自动派发
├── skill.py         技能加载
├── subagent.py      子Agent
└── compact.py       4级压缩管线 ← 新增
```

所有日志通过 `log_event()` 统一输出，工具调用日志由 Hook 自动触发。上下文压缩管线（L3 大结果落盘 → L1 消息裁剪 → L2 旧结果压缩 → LLM 摘要）防止 token 超限。

### v5 — 持久记忆 + 错误恢复

```
v5/
├── ...（v4 全部模块）
├── memory.py        跨会话持久记忆 ← 新增
│   └── 4个子系统: 存储(.memory/) · 加载(LLM侧查询) · 提取(每轮结束) · 合并(满10条)
└── recovery.py      错误恢复(3条路径) ← 新增
    └── Path1: max_tokens → 升级→continuation | Path2: prompt_too_long → 应急压缩 | Path3: 429/529 → 指数退避+fallback
```

### v6 — 任务系统

```
v6/
├── ...（v5 全部模块）
└── task_system.py   持久化DAG + 后台异步执行 ← 新增
    ├── 任务DAG: Task(id, subject, status, owner, blockedBy)
    ├── .tasks/ 目录存储JSON文件
    ├── can_start() 检查依赖 → complete_task() 报告解锁
    └── 后台执行: daemon线程 → threading.Lock → <task_notification> 通知注入
```

**新增工具：** `create_task` / `list_tasks` / `get_task` / `claim_task` / `complete_task`

### v7 — 多Agent协作

```
v7/
├── ...（v6 全部模块）
└── agent_teams.py   多Agent协作层 ← 新增
    ├── MessageBus: .mailboxes/.jsonl 文件邮箱 · 消费式读取
    ├── ProtocolState: request_id · type · status · match_response() 带类型验证
    ├── 两种协议: shutdown(优雅关闭) · plan_approval(计划审批)
    └── Teammate idle loop: 收件箱轮询 → 协议分发 → 持续待命
```

**新增工具：** `spawn_teammate` / `send_message` / `check_inbox` / `request_shutdown` / `request_plan` / `review_plan`

### v8 — 完整 Claude Code 架构

```
v8/
├── config.py              统一配置
├── llm.py                 LLM客户端
├── log.py                 统一日志(含WORKTREE/MCP类别)
├── main.py                入口 · 动态工具池 + MCP重组装
├── tools.py               核心工具(支持cwd参数)
├── hook.py                Hook系统
├── skill.py               动态system prompt(含MCP服务器信息)
├── subagent.py            子Agent
├── compact.py             上下文压缩
├── memory.py              持久记忆
├── recovery.py            错误恢复
├── task_system.py         任务系统 + 后台 + scan_unclaimed()
├── agent_teams.py         多Agent协作 + 自主Agent + worktree cwd
├── worktree_isolation.py  Git worktree任务隔离 ← 新增
└── mcp_plugin.py          MCP插件系统 ← 新增
```

**新增模块：**
- **worktree_isolation.py**：git worktree 任务绑定，队友在独立目录中工作，安全删除（检查未提交/未推送）。
- **mcp_plugin.py**：MCPClient 工具发现，`assemble_tool_pool()` 动态合并内置+MCP工具，`connect_mcp` 后即时可用。

**新增工具：** `create_worktree` / `remove_worktree` / `keep_worktree` / `connect_mcp` + 动态 `mcp__*` 工具

**关键改进：**
- **自主Agent**：队友 WORK→IDLE 循环，自动认领看板任务，不再完成即退出
- **动态工具池**：`agent_loop` 每次迭代使用 `assemble_tool_pool()` 合并内置+MCP工具

---

## 核心能力清单

### 工具

| 工具 | 功能 | 引入版本 |
|---|---|---|
| `bash` | 执行 shell 命令（120s 超时，支持 cwd + run_in_background） | v1 |
| `read_file` | 读取文件内容（支持 cwd） | v1 |
| `write_file` | 写入文件（支持 cwd） | v1 |
| `edit_file` | 精确文本替换 | v1 |
| `glob` | 文件模式搜索 | v1 |
| `spawn_subagent` | 派发子 Agent | v3 |
| `compact` | 手动触发上下文压缩 | v4 |
| `create_task` | 创建任务（可指定 blockedBy 依赖） | v6 |
| `list_tasks` | 列出所有任务及状态 | v6 |
| `get_task` | 获取任务详情 | v6 |
| `claim_task` | 认领任务（pending → in_progress） | v6 |
| `complete_task` | 完成任务，报告解锁的下游任务 | v6 |
| `spawn_teammate` | 启动自主队友 agent | v7 |
| `send_message` | 通过 MessageBus 发送消息 | v7 |
| `check_inbox` | 检查 Lead 收件箱 | v7 |
| `request_shutdown` | 请求队友优雅关闭 | v7 |
| `request_plan` | 要求队友提交计划 | v7 |
| `review_plan` | 审批/拒绝计划 | v7 |
| `create_worktree` | 创建隔离 git worktree | v8 |
| `remove_worktree` | 删除 worktree（安全检查） | v8 |
| `keep_worktree` | 保留 worktree 供审查 | v8 |
| `connect_mcp` | 连接 MCP 服务器，发现工具 | v8 |
| `mcp__*` | MCP 动态工具（连接后自动可用） | v8 |

### Hook 生命周期

| Hook | 触发时机 | 引入版本 |
|---|---|---|
| `UserPromptSubmit` | 用户输入前 | v2 |
| `PreToolUse` | 工具执行前（可阻断） | v2 |
| `PostToolUse` | 工具执行后 | v2 |
| `Stop` | Agent 即将退出（可强制继续） | v2 |
| `OnToolStart` | 工具执行前（自动派发，日志专用） | v4 |
| `OnToolEnd` | 工具执行后（自动派发，日志专用） | v4 |

### 技能

| 技能 | 功能 |
|---|---|
| `code_review` | 多维度代码审查（结构、风格、缺陷、安全、性能） |
| `data_analysis` | 数据分析与可视化建议 |
| `debug_helper` | 调试辅助（错误分析、根因定位、修复建议） |
| `translate` | 智能翻译（上下文感知、术语表支持） |

---

## 架构演进的深层规律

回顾从 v1 到 v8 的演变，可以总结出 LLM Agent 架构的数条规律。

### 横切关注点的外移

v1 的权限直接写在主循环中 → v2 通过 Hook 外移 → v4 的日志通过 Hook 自动派发 → v5 的记忆在 Hook 中注入。横切关注点从核心逻辑中**持续向外移动**，这是关注点分离原则的逐步实现。核心逻辑每轮迭代都变得更纯粹。

### 从中心化到去中心化

v3 的子 Agent 完全受主 Agent 控制（派发→执行→返回）→ v6 的任务依赖自动解锁 → v7 的 Teammate 持续待命 → v8 的自主 Agent 自己发现任务。控制权从 Leader 逐步下放给 Teammate——分布式系统的经典演化路径。

### 接口的标准化

v1 的工具定义是 ad-hoc 字典 → v2 的 Hook 注册表是简单列表 → v7 的协议有了结构化请求-响应 → v8 的 MCP 是标准化协议。接口从"隐式约定"向"显式契约"演化——软件工程成熟的标志。

### 五层架构的最终形态

```
扩展层: MCP 插件系统 (打破封闭性，外部工具接入)
协作层: 多Agent协作 + 自主Agent (并行工作，自组织)
任务层: 任务DAG + 后台执行 (工作的内容与顺序)
可靠性层: 记忆 + 恢复 + 压缩 + 日志 (稳定运行的基础)
核心层: agent_loop + 工具 + Hook + 技能 (Agent 的基本单元)
```

## 版本演进路线

| 版本 | 核心主题 | 关键新增模块 | 解决的核心矛盾 |
|---|---|---|---|
| v1 | 最简实现 | 3 文件 | 验证"LLM Agent 可以工作" |
| v2 | Hook + 技能 | hook.py, skill.py | 不修改主循环就能扩展能力 |
| v3 | 模块化 + 子 Agent | config.py, llm.py, subagent.py | 拆解单体 + 委派复杂任务 |
| v4 | 日志 + 压缩 | log.py, compact.py | 观察运行状态 + 防止上下文溢出 |
| v5 | 记忆 + 恢复 | memory.py, recovery.py | 跨会话知识存取 + 应对API故障 |
| v6 | 任务系统 | task_system.py | 管理多步骤依赖 + 慢操作不阻塞 |
| v7 | 多 Agent 协作 | agent_teams.py | 多Agent像团队一样并行工作 |
| v8 | 完整架构 | worktree_isolation.py, mcp_plugin.py | 自组织 + 文件隔离 + 插件扩展 |

## 各版本文档

- [v1/README.md](v1/README.md) — 最简实现：Agent Loop 基础
- [v2/README.md](v2/README.md) — Hook + 技能：关注点分离
- [v3/README.md](v3/README.md) — 模块化 + 子Agent：依赖反转
- [v4/README.md](v4/README.md) — 日志 + 压缩：可观测性
- [v5/README.md](v5/README.md) — 记忆 + 恢复：韧性工程
- [v6/README.md](v6/README.md) — 任务系统：DAG与异步
- [v7/README.md](v7/README.md) — 多Agent协作：消息传递
- [v8/README.md](v8/README.md) — 完整架构：自组织系统
