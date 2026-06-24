# Agent v8 — 完整 Claude Code 架构

## 1. 概述

v8 是完整的 Claude Code 架构复现，在 v7 基础上新增三个子系统：

- **自主 Agent**：队友不再被动等待分配，而是主动扫描任务看板、自动认领未分配的工作
- **Worktree 隔离**：并行工作的队友各自拥有独立的 git worktree，互不覆盖文件
- **MCP 插件系统**：通过标准协议发现和调用外部工具，无需修改代码即可扩展能力

至此，从 v1 的 3 个文件到 v8 的 15 个模块，完整复现了 Claude Code 的核心架构。

### 1.1 自组织系统与自主 Agent 的涌现行为

v7 的同事是被动的——它们等待 Lead 分配任务。v8 的自主 Agent 实现了**自组织行为**：每个 Teammate 在 idle 阶段主动扫描任务看板，独立判断"是否有我可以做的工作"。这看似微小的变化，在理论上有深远的意义。

**自组织系统的三个条件**：
1. 局部信息：每个 Agent 只看到任务看板的状态，不知道其他 Agent 的存在
2. 局部规则：每个 Agent 执行相同的规则——认领满足"pending + owner为空 + 依赖已满足"的任务
3. 全局行为涌现：所有任务最终被完成，且没有任务被重复执行

这不是中央调度器的指令，而是从局部规则中**涌现**的全局协调。这种设计的优势是：
- 如果增加 Agent 数量，系统自动加速（更多 Agent 认领更多任务）
- 如果某个 Agent 崩溃，其他 Agent 自动接手未完成的任务
- 不需要修改 Leader 的代码来适应不同数量的队员

**自动认领的竞态分析**：两个 Teammate 可能同时扫描到同一个未认领任务。v8 通过 `claim_task()` 的原子性来解决——虽然扫描可能重复，但认领是原子的（写入文件系统时只有一个能成功设置 owner）。第二个 Teammate 的 claim 会因"owner 不为空"而失败。

### 1.2 Worktree 隔离与并发安全

v7 的 Teammate 并行工作，但共享同一个工作目录。如果 Teammate A 在修改 `config.yaml` 的同时 Teammate B 也在修改 `config.yaml`，文件内容取决于写入顺序——这是竞态条件的典型表现。

v8 的 Worktree 隔离是**空间隔离**策略：每个任务绑定一个独立的 git worktree，Teammate 在认领任务时自动切换到对应的 worktree 目录。这与"用锁保护共享文件"的策略根本不同——不是控制访问顺序，而是**消除共享状态本身**。

**Git Worktree vs 复制目录**：
- 复制目录：每个目录有独立的 `.git`，重复存储（100MB 仓库 × 5 个 Teammate = 500MB）
- Git Worktree：所有 worktree 共享同一个 `.git` 对象数据库（100MB 仓库 × 5 个 worktree = 100MB + 各 worktree 的文件变更）
- Worktree 还提供原生的分支管理——每个 worktree 检出不同分支，合并回主分支时 git 的合并基础设施自动处理冲突

### 1.3 MCP 插件系统的开放性架构

v1-v7 的工具集是封闭的——所有工具都在 `tools.py` 中预定义。如果要添加一个新工具（如"查询数据库"），必须修改代码并重启 Agent。MCP 打破了这种封闭性。

**MCP 的架构创新**：它定义了一个**标准化的工具协议**，而不是标准化的工具实现。任何实现了 `tools/list` 和 `tools/call` 的服务都可以被 Agent 发现和调用。这类似于 USB 协议——不规定设备的功能，只规定通信方式。

`assemble_tool_pool()` 的设计是这种开放性的核心：它在每次 LLM 调用前动态构建工具列表（`BUILTIN + MCP`），而非使用编译时固定的工具列表。这意味着 `connect_mcp("docs")` 后，下一次 LLM 调用就能看到新工具——无需重启、无需重新加载代码。

**闭包陷阱的深层解释**：Python 的 `lambda` 捕获的是变量引用，而不是变量值。在循环中创建 lambda：

```python
# 错误：所有 lambda 共享同一个 client 和 tool_name（最后一次迭代的值）
for tool in mcp_tools:
    handlers[tool.name] = lambda **kw: client.call_tool(tool_name, kw)

# 正确：默认参数在定义时绑定当前值
for tool in mcp_tools:
    handlers[tool.name] = lambda *, c=client, t=tool.name, **kw: c.call_tool(t, kw)
```

这不是 Python 的 bug，而是 Python 的**设计选择**——lambda 作为闭包捕获外部作用域的变量引用。理解这一点对于正确实现任何"循环中创建函数"的场景至关重要。

## 2. 系统架构

![v8 完整系统架构图](architecture.svg)

*完整Claude Code架构：动态工具池(BUILTIN+MCP)、自主Agent(WORK/IDLE生命周期)、Worktree隔离(cwd切换)、MCP插件(工具发现+动态重组装)。从v1的3个文件到v8的15个模块。*

## 3. 文件结构

```
v8/
├── config.py              # 统一配置中心
├── llm.py                 # Anthropic 客户端单例
├── log.py                 # 统一日志（含 WORKTREE/MCP 类别）
├── main.py                # 入口：动态工具池 + MCP 重组装
├── tools.py               # 核心工具（支持 cwd 参数）
├── hook.py                # Hook 系统
├── skill.py               # 技能加载 + 动态 prompt（含 MCP 服务器信息）
├── subagent.py            # 子 Agent
├── compact.py             # 上下文压缩管线
├── memory.py              # 持久记忆
├── recovery.py            # 错误恢复
├── task_system.py         # 任务系统 + 后台执行 + 自动认领扫描
├── agent_teams.py         # 多 Agent 协作 + 自主 Agent + worktree cwd
├── worktree_isolation.py  # Worktree 隔离 ← 新增
└── mcp_plugin.py          # MCP 插件系统 ← 新增
```

## 4. 自主 Agent

### 4.1 设计理论

v7 的 Teammate 在完成初始任务后进入 idle loop 等待新消息。v8 的改进是让 Teammate 在 idle 阶段**主动扫描任务看板**，自动认领未分配的工作。这就是"自主"的含义——不需要 Lead 显式分配，Teammate 自己发现并认领任务。

**自组织系统的理论基础**：这种设计的理论根源是**自组织系统**（Self-Organizing Systems），其核心思想是：复杂系统的全局有序行为（所有任务被完成）可以从简单的局部规则中**涌现**，而不需要中央调度器。具体到 v8 的自主 Agent 中，这意味着：

- 每个 Teammate 拥有相同的局部规则：扫描看板 → 找到 pending + owner为空 + 依赖已满足的任务 → 认领 → 执行
- 每个 Teammate 只看到局部信息：任务看板的状态，不知道其他 Teammate 的存在和状态
- 全局行为自然涌现：所有任务最终被完成，且不会重复执行（因为认领会设置owner，其他Teammate无法再认领）

**与真实 Claude Code 的对比**：真实的 Claude Code 中，Agent 团队的工作分配同样遵循类似的自主原则。当一个 Agent 完成当前工作后，它会检查团队看板（team task board）寻找新的可认领任务。这种设计的优势在于：不需要一个专门的"调度器"来跟踪每个 Agent 的状态并分发任务——调度逻辑被分散到每个 Agent 的局部决策中。

**自组织 vs 中心化调度的架构权衡**：

中心化调度（如 v7 的 Lead 主动分配）的优势是对全局状态的完全可控——Lead 知道哪个 Agent 在执行哪个任务，可以基于负载均衡做出最优分配。但代价是 Lead 成为瓶颈——所有任务分配必须经过 Lead，Lead 的上下文窗口需要包含所有 Agent 的状态。

自组织调度（v8 的自主认领）的优势是去中心化——无需等待 Lead 分配，Teammate 自己发现工作。但代价是"非最优分配"——可能 Agent A 认领了一个它不太擅长的任务，而 Agent B（更擅长）却认领了另一个任务。Claude Code 通过**技能匹配**（skill matching）来缓解这个问题——任务描述中声明需要的技能，Agent 优先认领匹配自己技能的任务。

**为什么"从被动到主动"是质变而非量变**：v7 的 Teammate 在 idle 阶段只做一件事——轮询收件箱。如果没有新消息，它最终会超时退出。v8 的 Teammate 在 idle 阶段还有一个行为——扫描看板。这个增加的行为改变的是 Agent 的**自我驱动力**——v7 的 Agent 只在有人"叫"它时才工作，v8 的 Agent 会自己"找"工作。这种变化在软件架构中对应的是从**被动对象**到**主动智能体**的跃迁。

### 4.2 WORK/IDLE 生命周期

v8 的 Teammate 实现了完整的两阶段生命周期。理解这两个阶段的关系是理解自主 Agent 行为的关键。

**WORK 阶段的控制机制**：WORK 阶段最多执行 10 轮 LLM 调用。这个限制不是随意的——它基于对 LLM Agent 行为的观察：大多数工具型任务在 3-7 轮内完成。10 轮的上限给出了足够的探索空间，同时防止 Agent 陷入"无限探索"（LLM 不断调用工具但没有任何进展）。

真实的 Claude Code 中，WORK 阶段的轮次限制是动态调整的——简单任务（如"读一个文件"）可能在 1-2 轮完成，复杂任务（如"重构一个模块"）可能需要 15-20 轮。v8 使用固定上限是为了简化，但保留了通过 `request_plan` / `review_plan` 协议让 Lead 批准扩展轮次的能力。

**IDLE 阶段的轮询策略**：IDLE 阶段每 5 秒检查一次状态，最长持续 60 秒。这是一个**轮询（polling）** 策略，而非**事件驱动**策略。轮询的优势是实现简单——不需要信号量、条件变量或事件循环——文件系统本身就充当了"事件源"。Teammate 通过检查文件系统（收件箱文件是否存在、看板文件是否有变化）来判断是否有新工作。

轮询的代价是 CPU 空转和响应延迟——最坏情况下，一个可认领的任务可能需要等待 5 秒才能被 Teammate 发现。对于 Agent 协作场景（通信频率为"每秒数次"），5 秒的延迟是可接受的。如果场景需要更低的延迟（如实时协作），应该用事件通知机制（如 `inotify` 或消息队列）替代轮询。

**从 IDLE 回到 WORK 的三种触发路径**：
1. **收件箱新消息**：Lead 或其他 Teammate 发来了消息。这是显式的"有人叫我"路径
2. **看板有新任务**：扫描发现未认领的可执行任务。这是自主的"我找到工作了"路径
3. **60 秒超时**：既没有消息也没有新任务。这触发 SHUTDOWN——Agent 判断"暂时没有我的工作了"

这三种路径构成了 Agent 的完整行为谱系：被动响应 → 主动探索 → 自我终止。

### 4.3 身份重注入

身份重注入（Identity Re-injection）解决的是一个微妙的架构问题：**Agent 在上下文压缩后可能"失去身份"**。

**问题根源**：v4 引入了上下文压缩管线。压缩可能发生在对话的任何时刻——当 L0 压缩触发时，LLM 会将前 50 条消息压缩为一段摘要。如果早期的消息包含了 Agent 的身份描述（如"你是 Alice，一个数据库专家"），这个描述在压缩后的摘要中可能被简化或丢失。

更糟糕的是，Agent 可能不会"意识到"自己失去了身份——它继续工作，但可能使用了错误的角色设定（比如从"数据库专家"变成了"通用助手"），导致工作质量下降。

**检测机制**：v8 使用一个简洁的启发式方法来判断是否发生了上下文压缩——检查 `len(messages) <= 3`。正常运行的 Agent 的消息历史至少有数十条（用户的初始指令 + 多轮工具交互）。只有在压缩发生后（大部分消息被移除或替换为摘要），消息数才会骤降到 3 以下。

这个启发式方法虽然简单，但覆盖了关键的场景。真实的 Claude Code 使用更精确的检测——在压缩发生时设置一个标志位（`was_compacted: true`），而不是事后通过消息数量推断。

**重注入的内容和时机**：身份消息在 WORK 阶段的最开始注入——在收件箱检查之前、在 LLM 调用之前。这确保了 Agent 在处理任何新工作之前，都有完整的身份认知。身份消息包含 Agent 的名称和角色描述，但不包含任务上下文——任务上下文由后续的消息历史和收件箱消息提供。

### 4.4 队友工具集扩展

v7 的队友有 5 个工具（bash/read/write/send/plan），v8 扩展到 8 个。新增的三个工具是自主性的关键支撑：

| 新增工具 | 功能 | 架构意义 |
|---|---|---|
| `list_tasks` | 查看任务看板 | 赋予 Agent "了解全局"的能力，没有这个工具 Agent 无法知道有什么工作可做 |
| `claim_task` | 认领任务 | 赋予 Agent "主动获取工作"的能力，同时绑定 worktree 目录 |
| `complete_task` | 完成任务 | 赋予 Agent "独立完成"的能力，自动重置 worktree，触发下游任务解锁 |

**工具扩展的深层含义**：这三个工具让 Teammate 从"执行者"变成了"工作者"——执行者只做被分配的任务，工作者自己管理工作的完整生命周期（发现 → 认领 → 执行 → 完成）。这与人类工作方式的演变类似：初级工程师等待分配任务，高级工程师主动识别并认领工作。

**`claim_task` 的原子操作问题**：`claim_task` 需要同时做三件事——检查任务状态、设置 owner、切换 worktree 目录。在分布式系统中，这三步操作不是原子的。两个 Teammate 可能同时通过 `scan_unclaimed_tasks()` 发现同一个任务（扫描时 owner 都为空），然后先后调用 `claim_task`。

v8 的解决方案依赖于**文件系统的原子写入**——当两个 Teammate 同时写入同一个任务的 owner 字段时，文件系统的写入是原子的，只有一个能成功。第二个 Teammate 的 claim 会因为 "owner 不为空" 的条件检查而失败。这不是完美的分布式锁，但对于 Agent 协作场景（并发冲突概率低）是足够的。

## 5. Worktree 隔离

### 5.1 设计理论

v7 的多 Agent 协作让多个 Teammate 可以并行工作，但引入了一个新的问题：**文件系统竞态**。如果 Teammate A 在修改 `config.yaml` 的同时 Teammate B 也在修改同一个文件，最终的文件内容取决于写入的顺序和时间——这是典型的竞态条件。更微妙的是，即使两个 Teammate 修改不同的文件，共享的依赖（如 `node_modules`）也可能产生不一致。

这个问题在单 Agent 场景中不存在——因为只有一个 Agent 在工作，文件系统访问天然是串行的。一旦引入并行，文件系统的共享状态就变成了并发问题的根源。

**空间隔离 vs 锁控制的根本区别**：v8 选择了 **Git Worktree 空间隔离**而不是传统的锁控制。这两种策略代表了解决并发文件访问的两种根本不同的哲学：

锁控制（如 `flock`、读写锁）：允许多个 Agent 共享同一个工作目录，但通过锁来控制"谁在什么时候可以修改什么"。优势是简单——不需要额外的目录结构。劣势是锁引入了死锁风险、性能开销（Agent 等待锁释放）、和复杂性（细粒度锁的正确实现困难）。

空间隔离（Git Worktree）：每个 Agent 拥有完全独立的工作目录。Agent 之间的文件操作彼此完全不可见，消除了所有并发问题。优势是零并发风险、零锁开销。劣势是额外的磁盘空间（虽然有 git 对象共享来缓解）和需要在最后合并分支。

**为什么 Worktree 是"正确"的选择**：在 Agent 协作场景中，不同 Teammate 通常处理完全独立的任务（一个做认证模块、一个做 UI 界面），它们的工作几乎不需要交互。这意味着锁控制的大部分复杂性（处理同时修改同一文件的情况）是不必要的。空间隔离用最小的实现代价换来了最大的并发安全。

**Git Worktree 的底层机制**：Worktree 是 Git 的原生功能。它的关键特性是**共享对象数据库**——所有 worktree 共享主仓库的 `.git/objects` 目录，但拥有独立的工作目录和 HEAD。这意味着：
- 创建新 worktree 不需要复制整个仓库的 git 历史——只是创建一个新的指针到共享的对象数据库
- 在 worktree 中 `git checkout` 是独立的——一个 worktree 切换到 feature 分支不影响其他 worktree
- 合并回主分支时，Git 的合并基础设施自动处理冲突

真实的 Claude Code 中同样使用 worktree 或类似的隔离机制来确保并行 Agent 的文件安全。

### 5.2 目录拓扑

Worktree 的目录结构需要仔细设计，因为它是 Agent 代码和 Git 基础设施的交汇点：

```
Main repo (/)
  ├── .worktrees/
  │   ├── auth/          ← worktree 目录（独立工作空间）
  │   │   ├── .git       ← 指向主仓库对象数据库的指针文件
  │   │   ├── src/
  │   │   └── package.json
  │   ├── ui/            ← 另一个 worktree（完全独立）
  │   └── events.jsonl   ← 生命周期审计日志
  ├── .tasks/
  │   └── task_xxx.json  ← task 记录的 worktree 字段指向 "auth"
  └── src/               ← 主仓库的默认工作目录
```

**`.worktrees/` 与 `.git/worktrees/` 的区别**：标准的 Git Worktree 将元数据存储在 `.git/worktrees/`，但我们选择使用项目根目录下的 `.worktrees/`。原因是：`.git/` 目录通常不会被纳入代码 review 或备份，而 Agent 的工作产物（worktree 中的文件变更）是需要被管理的。将 worktree 放在项目根目录下，使它们与项目代码处于同一可见层级。

**任务与 Worktree 的绑定**：`.tasks/task_xxx.json` 中的 `worktree` 字段建立了任务与工作目录的绑定关系。当 Teammate 认领一个绑定了 worktree 的任务时，它自动切换到对应的目录。complete_task 时自动释放绑定。这种自动绑定机制消除了"Agent 选错工作目录"的人为错误。

### 5.3 队友 cwd 切换

cwd 切换是整个 Worktree 系统的核心机制。它的设计必须满足两个要求：对 Agent 代码透明（Agent 不需要知道自己在哪个 worktree 中工作）、对所有文件操作生效（不是选择性切换，而是全局切换）。

**`wt_ctx` 上下文管理器的设计**：Agent 的 `run()` 函数内部维护 `wt_ctx = {"path": None}`。这个简单的字典充当了**工作目录的运行时绑定**。所有文件操作在执行前都会解析 `wt_ctx["path"]`：

- `bash` → `subprocess.run(cmd, cwd=wt_ctx["path"] or WORKDIR)`
- `read_file` → `safe_path(filename, cwd=wt_ctx["path"] or WORKDIR)`
- `write_file` → 同上
- `edit_file` → 同上

**`safe_path` 的 cwd 参数增强**：v1-v7 的 `safe_path(p)` 只检查路径是否在工作区（`WORKDIR`）内。v8 的 `safe_path(p, cwd)` 接受可选的 `cwd` 参数——当 `cwd` 存在时，路径解析相对于 `cwd` 而非 `WORKDIR`。这个增强的关键在于：它仍然执行了完整的路径安全检查（防止 `..` 逃逸），但安全检查的"安全区域"从 `WORKDIR` 移到了 `cwd`。这意味着 Agent 在 worktree 中的文件操作被限制在该 worktree 内——不能意外访问其他 worktree 或主仓库。

**从 "全局路径" 到 "上下文相对路径" 的范式转变**：v1-v7 的路径模型是全局的——所有文件操作相对于 `WORKDIR`。v8 转变为上下文相对的——文件操作相对于当前 Agent 的工作上下文。这种转变反映了并行系统中"无共享"（shared-nothing）的设计哲学——每个 Agent 有自己的工作空间，彼此完全隔离。

### 5.4 安全机制

Worktree 系统引入了新的安全风险——主要是路径穿越（path traversal）和数据丢失风险。

**名称验证的深度分析**：`validate_worktree_name(name)` 不是简单的"检查非空"——它防止的是目录穿越攻击。考虑一个恶意的任务：如果任务 JSON 中的 `worktree` 字段是 `"../../etc"`，而 Agent 直接将这个值拼接到路径中，Agent 的文件操作可能逃逸到系统目录。

v8 的正则验证 `[A-Za-z0-9._-]{1,64}` 将 worktree 名称限制为安全的字符集和长度。这个看似简单的检查实际上是防止了整类攻击——所有不在白名单中的字符（包括 `/`、`..`、空字符）都被拒绝。

**安全删除的多重检查**：`remove_worktree(name, discard_changes)` 在删除前执行多重安全验证——这不是"很谨慎"，而是"必要的谨慎"。因为 worktree 包含了 Agent 独立完成的工作成果，一旦删除就无法恢复。

检查逻辑分为两层：
1. **未提交文件检查**（`git status --porcelain`）：如果 worktree 中有未提交的修改，说明 Agent 的工作成果尚未保存到 git。强制删除会永久丢失这些工作。
2. **未推送提交检查**（`git log @{push}..HEAD`）：如果有未推送的提交，说明工作成果虽然保存在 git 中，但没有同步到远程仓库。删除意味着失去了"恢复的最后一道防线"。

`discard_changes=True` 提供了强制删除的逃生口——但它的使用应该极其谨慎。在真实的 Claude Code 中，这个选项通常只在自动化测试清理或开发者明确确认后才使用。

**事件日志的审计价值**：`.worktrees/events.jsonl` 是 Worktree 系统的审计日志。它记录了每个 worktree 的创建、删除和保留事件，包括时间戳和关联的任务 ID。这看似简单的日志在实际运维中有重要价值——当需要排查"为什么某个文件丢失了"时，事件日志可以追溯到具体的 Agent 和任务操作。

## 6. MCP 插件系统

### 6.1 设计理论

v1-v7 的工具集是**封闭的**——所有工具都在 `tools.py` 中预定义和编译。如果要添加一个新工具（如"查询 PostgreSQL 数据库"），开发者必须修改 Agent 代码并重启 Agent。这个限制引发了两个问题：

1. **工具的领域限定**：Agent 只能使用预定义的通用工具（bash/read/write/edit/glob），无法访问特定领域的数据和服务（如公司内部的工单系统、监控面板、知识库）
2. **工具的维护耦合**：每次添加新工具都需要修改核心代码（`tools.py` 和 `main.py`），工具的实现与 Agent 的核心逻辑混在一起

MCP（Model Context Protocol）是 Anthropic 提出的解决方案——通过标准化的协议，让**外部工具服务器**可以被 Agent 动态发现和调用，**无需修改 Agent 的核心代码**。

**MCP 作为"工具层面的依赖反转"**：在 v1 的架构中，Agent 直接依赖工具实现（`TOOL_HANDLERS` 字典）。MCP 将这个依赖关系反转——Agent 依赖抽象的工具协议（`tools/list` + `tools/call`），具体的工具实现由外部 MCP 服务器提供。这与 v3 中通过 `config.py` 实现配置的依赖反转是同一个原则：**依赖抽象而非具体实现**。

**MCP 协议的层次结构**：MCP 协议不是单一协议，而是一个**协议族**：
- **发现层**（`tools/list`）：Agent 向服务器查询"你能提供什么工具？"
- **调用层**（`tools/call`）：Agent 向服务器发送"请执行工具 X，参数为 Y"
- **传输层**：v8 的教学版使用内存中的直接调用（`MCPClient.register()`），真实的 Claude Code 使用 stdio（本地进程通信）或 SSE/HTTP（远程服务通信）

**为什么协议比 API 更适合工具集成**：传统的 REST API 是单向的——客户端发送请求，服务端返回响应。MCP 协议是双向的——Agent 和 MCP 服务器之间有持续的会话。这意味着 MCP 服务器可以维护状态（如数据库连接的连接池），而 REST API 每次请求都是无状态的。对于需要持久连接的场景（如数据库查询、文件系统监控），协议的持续会话模型更合适。

### 6.2 MCPClient 类

`MCPClient` 是 v8 中对 MCP 协议的简化实现。它不是通过 stdio/SSE 与外部进程通信，而是在内存中维护 `_handlers` 字典来模拟 MCP 服务器的行为。

```python
class MCPClient:
    name: str               # 服务器标识（如 "docs"、"deploy"）
    tools: list[dict]       # 工具定义列表（name, description, inputSchema）
    _handlers: dict          # 工具名 → callable

    register(tool_defs, handlers)  # 注册工具定义和实现（模拟 tools/list）
    call_tool(name, args)          # 调用指定工具（模拟 tools/call）
```

**教学版与真实版的差异**：v8 的 `MCPClient` 是教学版本——它通过内存中的 handler 字典来"模拟"MCP 服务器。真实的 Claude Code 中，`MCPClient` 的实现要复杂得多：

- **进程管理**：启动和管理外部 MCP 服务器进程（通过 stdio）、处理进程崩溃和重启
- **协议序列化**：将工具调用序列化为 JSON-RPC 2.0 格式（MCP 的实际通信格式），通过 stdin 发送，从 stdout 读取
- **传输多样性**：支持 stdio（本地）、SSE（远程）、WebSocket 三种传输方式
- **认证鉴权**：验证 MCP 服务器的身份、管理访问令牌

但核心抽象是相同的——`MCPClient` 封装了"与一个工具服务通信"的复杂性，向 Agent 暴露简单的 `call_tool(name, args)` 接口。理解了这个核心抽象，理解真实的实现就是理解传输细节和错误处理。

### 6.3 工具命名规范

工具命名遵循 `mcp__<normalized_server>__<normalized_tool>` 的规范。这个规范不是随意选取的，它解决了三个问题：

**命名冲突**：内置工具和 MCP 工具可能同名。比如内置工具是 `search`（搜索文件内容），docs MCP 服务器的工具也是 `search`（搜索文档），如果没有前缀区分，两个工具会发生命名冲突。`mcp__docs__search` 明确表示这是 docs MCP 服务器的 search 工具。

**来源追溯**：当 Agent 调用 `mcp__deploy__trigger` 触发了一次部署，日志中可以清晰地看到这个操作来自 deploy MCP 服务器。在调试和审计场景中，知道工具的来源至关重要——一个错误的文件操作如果是内置工具执行的，是 Agent 的问题；如果是 MCP 服务器提供的工具执行的，需要检查 MCP 服务器的实现。

**注入防御**：`normalize_mcp_name(name)` 将所有非 `[a-zA-Z0-9_-]` 字符替换为下划线。这看似简单的操作防止了**命名注入攻击**——如果 MCP 服务器的名称包含特殊字符（如 `docs; rm -rf /`），不加规范化的工具命名可能导致意外行为。规范化限制了工具名称的字符集，从根本上消除了注入风险。

**命名规范的局限性**：双下划线分隔符（`__`）在理论上可能与合法的 MCP 工具名称冲突（如果一个工具本身就包含 `__`）。真实的 MCP 协议通过 namespacing 而非字符串拼接来解决这个问题——每个工具有独立的命名空间（server id），而非编码在字符串中。

### 6.4 动态工具池

`assemble_tool_pool(builtin_tools, builtin_handlers)` 是 v8 架构的核心创新——它在**运行时构建工具列表**，而非使用编译时固定的工具列表。

**静态工具池的局限**：v1-v7 的工具列表是全局常量 `TOOLS`。在 `agent_loop` 中，每次 LLM 调用都传递同一个 `TOOLS`。这意味着：
- 无法在运行时添加新工具（工具列表在代码中硬编码）
- 无法在运行时移除工具（即使某个 MCP 服务器断开了，它的工具仍然在列表中）
- LLM 始终看到所有工具，即使某些工具在当前上下文中不相关

**动态池的运行时行为**：`assemble_tool_pool()` 的执行逻辑分为三步：
1. **复制内置工具**：内置工具（bash/read/write/etc）始终可用，因为它们不依赖外部服务
2. **遍历 MCP 客户端**：对于每个已连接的 MCP 客户端（如 docs 服务器、deploy 服务器），提取其工具定义
3. **添加前缀并注册 handler**：为每个 MCP 工具添加 `mcp__<server>__` 前缀，创建对应的 handler（lambda 函数）

关键设计是：**动态池在每次 `connect_mcp` 后重建，在每次 LLM 调用时使用最新的池**。这意味着：
- `connect_mcp("docs")` 后，下一次 LLM 调用就能使用 `mcp__docs__search`
- 如果 docs 服务器断开，下一次重建会排除它的工具

**闭包陷阱与 Python 的变量捕获**：`assemble_tool_pool()` 中最微妙的部分是闭包陷阱的处理。在循环中为每个 MCP 工具创建 lambda handler 时：

```python
# 正确做法：默认参数在定义时绑定当前值
for tool in mcp_tools:
    handlers[name] = lambda *, c=client, t=tool["name"], **kw: c.call_tool(t, kw)
```

如果写成 `lambda **kw: client.call_tool(tool_name, kw)`（省略默认参数绑定），所有 lambda 会在**调用时**去查找循环变量 `tool_name` 的值——而那时循环已经结束，`tool_name` 是最后一个工具的名称。所有 MCP 工具无论名字如何，都会调用同一个工具。

这是 Python 作用域规则的结果——lambda 作为闭包捕获的是变量的**引用**而非**值**。在 C++ 中类似的问题通过 `[=]` vs `[&]` 捕获列表解决，在 JavaScript 中通过 `let` vs `var` 解决。理解这个陷阱对于任何"在循环中创建函数"的场景都至关重要。

### 6.5 与主循环的集成

MCP 插件系统与主循环的集成涉及两个关键点：工具池的动态更新和 system prompt 的同步刷新。

**工具池更新时机**：`connect_mcp` 工具被执行后，`main.py` 中的 agent_loop 执行以下逻辑：

1. 重新调用 `assemble_tool_pool()` 构建新的工具列表（包含新连接的 MCP 工具）
2. 更新 `current_tools` 和 `current_handlers` 引用
3. 重新调用 `get_system_prompt()` 刷新 system prompt（使其包含新工具的描述）

**为什么必须在 `connect_mcp` 后立即重建而不是在下一轮**：如果不在当前轮立即更新工具池，接下来的 LLM 调用仍然传递旧的工具列表——LLM 不知道新工具的存在，自然无法使用。`connect_mcp` 的返回值（"已连接 docs 服务器，发现了 2 个工具"）会被附加到消息历史中，LLM 在下一轮就能看到 MCP 工具的描述。

**system prompt 刷新的必要性**：工具列表不只是传递给 LLM 的 `tools` 参数——工具的详细描述也存在于 system prompt 中（通过技能系统和 system prompt 模板）。如果只更新 `tools` 参数而不刷新 system prompt，LLM 知道工具有哪些参数，但不知道工具的使用场景和限制。同步刷新确保了 LLM 对工具的理解是完整的。

**工具池的生命周期管理**：动态工具池的生命周期与 agent_loop 的迭代周期一致。`current_tools` 和 `current_handlers` 在每次 agent_loop 迭代开始时可能被更新（如果有 `connect_mcp` 调用）。这是一个"延迟更新"策略——不是在 MCP 服务器连接/断开的瞬间更新，而是等待到下一轮 LLM 调用前。这种策略的优势是批量更新——如果同一轮连接了多个 MCP 服务器，只需要一次 `assemble_tool_pool()` 调用。

## 7. 完整工具清单

| 类别 | 工具 | 数量 |
|---|---|---|
| **核心** | bash / read_file / write_file / edit_file / glob | 5 |
| **扩展** | compact / spawn_subagent | 2 |
| **任务** | create_task / list_tasks / get_task / claim_task / complete_task | 5 |
| **团队** | spawn_teammate / send_message / check_inbox / request_shutdown / request_plan / review_plan | 6 |
| **Worktree** | create_worktree / remove_worktree / keep_worktree | 3 |
| **MCP** | connect_mcp + mcp__* 动态工具 | 1+N |
| **总计** | | 22+N |

## 8. 迭代回顾

| 版本 | 核心贡献 | 解决的问题 |
|---|---|---|
| v1 | 最简实现 | 验证 API 可用性 |
| v2 | Hook + 技能 | 关注点分离、能力扩展 |
| v3 | 模块化 + 子 Agent | 配置集中、任务分解 |
| v4 | 日志 + 压缩 | 可观测性、可持续性 |
| v5 | 记忆 + 恢复 | 长期知识、鲁棒性 |
| v6 | 任务系统 | 顺序约束、执行效率 |
| v7 | 多 Agent 协作 | 并行工作、跨线程通信 |
| v8 | 自主 + 隔离 + 插件 | 自组织、文件隔离、外部扩展 |

从 3 个文件到 15 个模块，从直接调用到 Hook 编排，从单 Agent 到多 Agent 协作，从同步执行到后台异步，从固定工具到动态插件——这就是一个完整 Claude Code 架构的构建过程。

### 8.1 完整架构的"收敛性"思考

v8 的 15 个模块不是随意堆砌的——它们形成了一个**层次化架构**，每层建立在下层之上：

```
第5层 — 扩展层: MCP 插件系统 (为封闭系统打开外部接口)
第4层 — 协作层: 多Agent协作 + 自主Agent (多实例并行工作)
第3层 — 任务层: 任务系统 + 后台执行 (管理工作的内容和顺序)
第2层 — 可靠性层: 记忆 + 恢复 + 压缩 + 日志 (确保系统稳定运行)
第1层 — 核心层: agent_loop + 工具 + Hook + 技能 (Agent 的基本能力)
```

这种层次结构反映了软件架构的通用分层模式——底层提供基础能力，上层提供高级抽象。每一层都可以独立演化：你可以改进压缩算法而不影响任务系统，可以添加新的 MCP 服务器而不修改 agent_loop。

### 8.2 架构的"未完成性"

v8 复现了 Claude Code 的**核心架构模式**，但真实的 Claude Code 还有 v8 没有实现的特性。这不是缺陷——v8 的目的是**展示架构演进的过程和原理**，而非实现一个完整的商业产品。理解这些差距本身也是架构学习的一部分：

**Sandbox 隔离**：真实的 Claude Code 在更严格的沙箱中执行工具调用（文件系统虚拟化、网络限制、进程隔离），而非直接使用主机的文件系统和网络。v8 的 worktree 隔离是文件系统级别的，但网络和进程隔离需要操作系统级别的机制。

**持续化进程**：真实的 Claude Code 以常驻进程运行（类似于 IDE 的后台服务），而非每次用户输入都重新启动。这需要更复杂的状态管理——哪些状态应该在进程重启后保留？哪些应该丢弃？

**IDE 集成**：真实的 Claude Code 与编辑器深度集成（如 VS Code 的 diff 视图、内联建议），而非通过命令行交互。这涉及前端架构和编辑器插件协议（如 LSP）的知识。

v8 的教学版本选择性地省略了这些复杂性，聚焦于 Agent 本身的架构设计——这是合理的选择，因为 Sandbox 和 IDE 集成属于不同的工程领域。

### 8.3 从 v1 到 v8 的架构演变规律

回顾 8 个版本的演变，可以总结出 LLM Agent 架构的几条规律：

**① 从"能做"到"做好"的渐进式完善**

v1 证明了可行性（"Agent 可以工作"），v2-v8 逐步完善了可用性（"Agent 可以长期使用、多人协作、动态扩展"）。这不是一蹴而就的设计，而是**增量演化**——每个版本解决前一个版本暴露的具体问题。

**② 横切关注点的持续外移**

v1 的权限直接写在主循环中，v2 通过 Hook 外移，v4 的日志通过 Hook 自动派发，v5 的记忆在 Hook 中注入。横切关注点从核心逻辑中**持续向外移动**，这是关注点分离原则的逐步实现。

**③ 从中心化到去中心化的权力转移**

v3 的子 Agent 完全受主 Agent 控制（派发→执行→返回），v6 的任务系统引入了依赖自动解锁，v7 的 Teammate 可以持续待命，v8 的自主 Agent 自己发现任务。控制权从中心（Leader）逐步下放给边缘（Teammate），这是分布式系统设计的经典轨迹。

**④ 接口设计的逐步标准化**

v1 的工具定义是 ad-hoc 字典，v2 的 Hook 注册表是简单的列表，v7 的协议有了结构化的请求-响应格式，v8 的 MCP 是标准化协议。接口从"隐式约定"向"显式契约"演化——这是软件工程成熟的标志。
