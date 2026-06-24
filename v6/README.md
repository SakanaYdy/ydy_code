# Agent v6 — 任务系统 + 后台执行

## 1. 概述

v6 新增**持久化任务依赖图**和**后台异步执行**。任务系统解决了"多步骤项目的顺序约束"问题——你不能先盖屋顶再打地基。后台执行解决了"慢操作阻塞主循环"问题——你不需要站在洗衣机前等 30 分钟。

这两个子系统分别关注**任务编排**和**执行效率**，是 Agent 从"单轮对话工具"向"项目管理助手"演进的关键步骤。

### 1.1 有向无环图的本质含义

v6 的 Task DAG 不是"高级数据结构的应用"，而是**将 Agent 的推理认知外化为显式约束**。

在 v6 之前，如果用户说"帮我做一个登录功能"，Agent 需要**在推理过程中自行维护**"先建数据库、再写API、最后做UI"的顺序。这个顺序存在于 LLM 的推理链中，而不是系统的任何数据结构中。一旦 LLM 的注意力转移（比如被迫压缩上下文），这个顺序就会丢失。

DAG 将顺序约束从 LLM 的推理中**提取到系统层面**。`blockedBy: ["task_001", "task_002"]` 是一个显式约束——不管 LLM 是否记得，系统都会阻止 task_003 在依赖完成前被认领。

**DAG vs 线性列表的本质区别**：

```
线性列表: [建数据库, 写API, 做UI]
  问题: 写API和做UI没有依赖关系，但列表暗示了顺序

DAG:
  建数据库 → 写API
           → 做UI
  表述: 写API和做UI都可以在建数据库完成后开始
```

DAG 揭示了**并行机会**——而线性列表隐藏了并行机会。当多个 Agent 协同工作时（v7/v8），DAG 中的并行分支可以被分配给不同的 Agent 同时执行。

### 1.2 后台执行与异步通知的架构含义

v6 的后台执行引入了一个深层的架构变化：**Agent 的工具调用从"同步阻塞"变成了"异步非阻塞"**。这改变了 Agent 的行为模式：

**同步模式**（v1-v5）：
```
LLM调用 → 判断tool_use → 执行工具(等待...) → 获取结果 → 下一轮LLM
Agent 在等待工具执行时完全停滞
```

**异步模式**（v6）：
```
LLM调用 → 判断tool_use → 启动后台线程 → 立即返回占位结果 → 继续下一轮LLM
Agent 不等待工具完成，继续处理其他任务
(稍后) → 后台完成 → <task_notification>注入 → LLM得知结果
```

这种变化的核心是**解耦了"发起操作"和"获取结果"的时间点**。Agent 不需要"盯着水壶等水开"，而是在水烧开后通过通知得知。

## 2. 系统架构

![v6 系统架构图](architecture.svg)

*主循环通过 should_run_background() 判断慢操作，后台daemon线程执行不阻塞主流程。任务系统以 DAG 管理顺序约束，自动解锁下游任务。*

## 3. 文件结构

```
v6/
├── config.py        # 统一配置中心
├── llm.py           # Anthropic 客户端单例
├── log.py           # 统一日志
├── main.py          # 入口：agent_loop + 后台分发
├── tools.py         # 5 个核心工具
├── hook.py          # Hook 系统
├── skill.py         # 技能加载 + 动态 prompt
├── subagent.py      # 子 Agent
├── compact.py       # 上下文压缩管线
├── memory.py        # 持久记忆
├── recovery.py      # 错误恢复
└── task_system.py   # 任务系统 + 后台执行 ← 新增
```

## 4. 任务系统

### 4.1 设计理论

任务系统的设计动机是**管理多步骤项目的顺序约束**。TodoWrite（内存清单）只有"做什么"，没有"先做什么后做什么"。任务系统通过 `blockedBy` 依赖关系形成**有向无环图**（DAG），确保任务按正确顺序执行。

**DAG 作为"推理外化"的架构意义**：在 v6 之前，Agent 在推理过程中自行维护"先建数据库、再写 API、最后做 UI"的依赖顺序。这个顺序存在于 LLM 的潜在推理链中，不在系统的任何数据结构中。一旦 LLM 被迫压缩上下文（v4 的管线），这个隐式顺序就丢失了——Agent 可能"忘记"某个任务依赖于另一个任务的完成。

DAG 将依赖关系**从 LLM 的推理中提取到系统层面**。`blockedBy: ["task_001", "task_002"]` 是一个显式的、不可丢失的约束。不管 LLM 的记忆被压缩了多少次，系统都会阻止 task_003 在依赖完成前被认领。这是从"依赖推理"到"依赖保证"的质变。

**DAG 揭示的并行机会**：线性任务列表（[建数据库, 写 API, 做 UI]）隐藏了并行可能——写 API 和做 UI 都依赖建数据库，但彼此不依赖。DAG 的结构使得 `can_start()` 可以并行检查多个任务——建数据库完成后，写 API 和做 UI 同时变为"可开始"状态。当 v7/v8 引入多 Agent 协作时，这两个并行的任务可以被分配给不同的 Teammate 同时执行。

**`can_start()` 的保守设计**：`can_start()` 将所有 `blockedBy` 依赖的 `completed` 状态作为必要条件。这比"检查是否有任何依赖是 pending 或 in_progress"更严格——如果某个依赖任务是"缺失的"（在 blockedBy 中提到但不在 .tasks/ 中），`can_start()` 返回 False。这个保守设计防止了"引用了不存在的任务"导致的静默错误。在分布式系统中，保守的依赖检查比宽松的检查更安全。

**Claude Code 的任务管理对比**：真实的 Claude Code 使用 TaskCreate/TaskUpdate/TaskList 等工具管理任务。v6 复现的是这个任务模型的核心——DAG 依赖、状态机流转、文件持久化。但 Claude Code 的任务系统更丰富——支持任务优先级、deadline、跨 workspace 的任务依赖、以及任务模板（常见任务模式的预设）。

### 4.2 任务数据结构

`Task` dataclass 是任务系统的数据核心。每个字段的选择都对应任务管理的一个维度：

```python
@dataclass
class Task:
    id: str            # 唯一标识，格式"task_{timestamp}_{random}"
    subject: str       # 简短标题，给人类和LLM快速判断任务内容
    description: str   # 详细描述，给LLM理解任务的完整上下文
    status: str        # 状态机核心：pending/in_progress/completed
    owner: str | None  # Agent名称，v7/v8多Agent时使用
    blockedBy: list    # 依赖任务ID列表，形成DAG
```

**`id` 的生成规则**：`task_{timestamp}_{random}` 格式确保唯一性——时间戳保证时间序，随机数防止同一时间戳的冲突。为什么不直接用 UUID？因为时间戳提供了"创建时间"信息，方便调试和运维中判断任务年龄。

**`subject` vs `description` 的分离**：subject 是给"快速浏览"用的（列出任务列表时），description 是给"深入理解"用的（认领任务后阅读）。这模拟了看板系统——卡片标题一眼可见，展开看详情。LLM 在 `list_tasks` 时能快速了解所有任务，在 `get_task` 时获得完整上下文。

**`owner` 的 None 默认值**：owner 为 None 表示"未认领"。v6 中 owner 是主 Agent 的名称，v7 中是 Teammate 的名称，v8 中由自主 Agent 的 `claim_task` 设置。owner 字段从可选（v6）到必需（v7/v8）的演变反映了从单 Agent 到多 Agent 的架构进化。

### 4.3 状态机

状态机的流转规则编码了任务管理的约束：

```
pending ──claim──→ in_progress ──complete──→ completed
```

**`claim` 的前置条件（两层检查）**：
1. 任务状态必须是 `pending`——不能认领已完成或在执行中的任务
2. 所有 `blockedBy` 依赖必须是 `completed`——`can_start()` 验证

如果 `blockedBy` 中有"不存在的任务ID"（其他任务引用的依赖从未被创建），`can_start()` 返回 False。这是防御性设计——不假设数据完整性，对任何缺失都采取保守策略。

**`complete` 的后置动作**：完成后自动扫描所有 `pending` 任务，对每个任务调用 `can_start()`。所有"现在可以开始了"的任务被标记为"已解锁"，通知给 Agent。这个自动解锁机制是 DAG 的核心价值——手动解锁在任务多时极易遗漏。

**状态机的不完整之处**：v6 没有 `failed` 状态。如果任务执行失败，它只能停留在 `in_progress` 或 force-complete。这在实际使用中会导致"看起来在做但永远做不完"的任务阻塞下游。v8 没有解决这个问题——真实 Claude Code 的任务状态更丰富（包括 cancelled、failed、skipped）。

### 4.4 文件持久化

每个任务序列化为 `.tasks/{id}.json` 文件存储。选择 JSON 格式而非 pickle 或数据库是因为：
- JSON 人类可读——可以直接打开文件查看任务状态
- JSON 跨语言——如果将来用其他语言实现工具（如 TypeScript 的 IDE 插件），可以直接读取
- 文件系统是无依赖的数据库——不需要安装和维护任何数据库软件

**跨会话恢复**：Agent 重启后读取 `.tasks/` 目录，将所有 JSON 文件反序列化为 Task 对象。已完成的任务保留在磁盘上（提供历史记录），但只有 pending 和 in_progress 的任务被加载到内存的活跃任务列表中。

### 4.5 与 TodoWrite 的对比

| 维度 | TodoWrite（内存清单） | Task System（持久化 DAG） |
|---|---|---|
| 存储 | 进程内 | `.tasks/{id}.json` |
| 依赖 | 无 | `blockedBy` 图 |
| 生命周期 | 当前会话 | 跨会话 |
| 协作 | 无认领机制 | `owner` / `claim` |
| 解锁 | 手动 | 自动报告 |

## 5. 后台异步执行

### 5.1 设计理论

v1-v5 的工具执行是**同步阻塞**的——Agent 发起 `bash "npm install"` 后，主循环被阻塞，等待安装完成（可能数分钟）。这期间 Agent 无法做任何其他事情——不能继续推理、不能处理其他工具调用、甚至不能响应 shutdown 请求。

**同步 vs 异步的本质区别**：同步执行是"我在等结果"——调用方的时间线和被调用方的时间线耦合。异步执行是"我等结果到达"——调用方的时间线和被调用方的时间线解耦。v6 的后台执行实现了这种解耦——Agent 发起操作后继续工作，操作完成的结果通过通知机制异步返回。

**两级判断的设计**：后台执行的决策采用两级判断——模型显式请求（`run_in_background: true`）和启发式匹配（慢操作关键词）。模型显式请求优先，因为它基于 LLM 对任务的语义理解（"npm install 一般需要 2-3 分钟，应该后台执行"），比关键词匹配更准确。关键词匹配是 fallback——当模型没有显式标记但命令明显是慢操作时。

**为什么不把所有操作都后台执行**：后台执行有代价——Agent 不能立即获取结果。如果 Agent 需要文件内容来决定下一步操作（`read_file config.yaml`），后台执行会延迟决策。同步执行适合"需要立即结果"的操作，后台执行适合"结果不需要立即使用"的操作。

### 5.2 执行流程

后台执行的完整生命周期分为四个阶段：

**阶段一：判断**。`should_run_background(block)` 检查两个条件——block 中是否有 `run_in_background: true` 参数（LLM 显式要求）、或 command/description 中是否包含慢操作关键词。任一条件为 true 即进入后台。

**阶段二：分发**。`start_background_task(block, handler)` 创建 daemon 线程，线程内部执行 `handler(**block.input)`。daemon 线程的特点是在主线程退出时自动终止——Agent 退出时不需要手动清理后台线程。

**阶段三：占位**。主循环不等待后台线程完成，立即返回占位 tool_result `"[Background task bg_0001 started] Command: npm install..."`。LLM 收到这个占位结果，知道操作已启动但结果待定，继续处理其他工作。

**阶段四：通知**。每轮循环的 `collect_background_results()` 检查 `background_tasks` 字典中 `status == "completed"` 的条目。将结果包装为 `<task_notification>` XML 注入到下一轮 user message 中。LLM 看到通知，知道后台任务的结果，整合到后续推理中。

### 5.3 通知格式

通知格式的设计解决了"如何区分原始请求和后台完成通知"的问题：

```xml
<task_notification>
  <task_id>bg_0001</task_id>
  <status>completed</status>
  <command>pip install torch</command>
  <summary>Successfully installed torch-2.1.0</summary>
</task_notification>
```

**为什么不复用原始 tool_use_id**：Anthropic Messages API 要求一个 `tool_use` 块只能对应一个 `tool_result` 块。后台任务启动时已经发送了占位 tool_result（如 "Background task started"），不能再发送第二个 tool_result。后台完成的结果必须通过独立的 user message 注入，使用新的 `task_id`（如 `bg_0001`）关联到原始请求。

**XML 格式而非纯文本的原因**：XML 提供了明确的结构边界——LLM 知道 `<task_notification>` 是系统通知，不是用户说的话。与记忆注入（v5 的 `<relevant_memories>`）一致，XML 标签是系统注入信息的标准格式。

### 5.4 线程安全

后台执行引入了多线程环境，`background_tasks` 和 `background_results` 两个字典被多个线程同时访问。

**竞态风险分析**：后台线程在任务完成时写入 `background_tasks[id]["status"] = "completed"` + 写入 `background_results[id]`，主循环在 `collect_background_results()` 中读取这两个字典。如果不在写入时加锁，主循环可能读到"status 是 completed 但 result 还是空"的不一致状态。

**`threading.Lock` 的保护机制**：所有对 `background_tasks` 和 `background_results` 的读写都通过 `background_lock.acquire()` / `release()` 保护。锁的粒度是全局的（整个字典），而不是每条记录单独一把锁。全局锁实现简单，在 Agent 场景中足够（后台任务数量少，锁竞争概率低）。

### 5.5 慢操作关键词

```python
SLOW_KEYWORDS = ["install", "build", "test", "deploy", "compile",
    "docker build", "pip install", "npm install", "cargo build", ...]
```

**关键词选择的工程依据**：这些关键词来自对常见软件工程操作的时间经验——包管理操作（pip/npm/apt/yum install）通常 30s-5min，编译操作（mvn/gradle/make/build）通常 1-10min，容器操作（docker build）通常 2-15min。包含具体的命令名称（如 `pip install`）和通用动词（如 `install`），兼顾精确匹配和泛化。

**为什么这是 fallback 而非主要判断**：关键词匹配可能误判——`echo "run npm install" > script.sh` 包含 `npm install` 但不应该后台执行。LLM 的显式标记更准确，因为它基于语义理解而非字符串匹配。真实 Claude Code 主要依赖 LLM 的 `run_in_background` 标记，关键词只是少数不支持此参数的兼容性 fallback。

## 6. 与 v5 的对比

| 维度 | v5 | v6 |
|---|---|---|
| 任务管理 | 无（TodoWrite 内存清单） | 持久化 DAG（.tasks/ 目录） |
| 执行模型 | 全部同步 | 慢操作后台执行 |
| 依赖管理 | 无 | blockedBy + can_start() |
| 跨会话 | 任务丢失 | 文件持久化，重启恢复 |
| 工具数 | 7 | 12（+5 任务工具） |
| 模块数 | 11 | 12 |

v6 的任务系统和后台执行是 v7/v8 多 Agent 协作的基础——任务认领和分布式执行都依赖于此。

### 7.1 后台执行的"部分完成"问题

异步执行引入了一个复杂的状态管理问题：后台任务可能**部分完成**。例如 `npm install` 启动了，但在安装过程中 Agent 退出了。daemon 线程随主线程终止而终止，但文件系统可能处于不一致状态（部分依赖已安装、部分未安装）。

v6 对此没有处理——它接受"Agent 退出时可能有未完成的后台任务"。这是一个务实但有风险的设计。生产系统需要：
- 后台任务的状态持久化（重启后检查并恢复）
- 事务性任务执行（完成 or 回滚，没有中间状态）
- 超时机制（后台任务不能无限运行）

### 7.2 DAG 的"依赖死锁"问题

DAG 本身保证无环，但在实际操作中可能出现**逻辑死锁**——不是图的死循环，而是任务的现实不可行性：

```
Task A: 安装 Python 依赖  (blockedBy: [])
Task B: 运行 Python 测试  (blockedBy: [A])
Task C: 配置 Docker 环境 (blockedBy: [B])
Task D: 构建 Docker 镜像 (blockedBy: [C, A])
```

Task D 依赖 Task C 和 Task A。但如果 Task C 失败了（Docker 未安装），Task D 永远无法开始——不是因为 DAG 有 bug，而是因为**依赖链中的某个环节失败导致下游全部阻塞**。

v6 对此没有处理——任务状态只有 pending/in_progress/completed，没有 failed/blocked。真实的 Claude Code 通过更丰富的任务状态（包括 cancelled、failed、skipped）和依赖重评估（失败的上游是否真的阻塞下游？）来解决这个问题。
