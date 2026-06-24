# Agent v7 — 多 Agent 协作

## 1. 概述

v7 新增**多 Agent 协作层**，解决单个 Agent 上下文窗口无法覆盖大型任务所有模块的问题。核心思想是"一个项目经理（Lead）+ 多个开发者（Teammate）"：Lead 负责任务分解和协调，Teammate 在后台线程中并行执行子任务，通过文件邮箱通信。

协作层包含三个子系统：**消息总线**（跨线程通信）、**协议状态机**（结构化请求-响应）、**队友 idle loop**（等待收件箱消息而非固定轮次退出）。

### 1.1 多 Agent 协作的架构选择

多 Agent 系统的核心问题是**通信机制**。v7 面临两个选择，每个选择都有深层含义：

**选择一：共享内存**

所有 Agent 读写同一块内存区域。优势是速度极快（纳秒级），劣势是**竞争条件地狱**——两个 Agent 同时修改同一个任务状态时，需要锁、原子操作、事务。更重要的是，共享内存假设所有 Agent 在同一个进程中——这限制了分布式部署的可能。

**选择二：消息传递**

v7 选择了消息传递——每个 Agent 有独立的收件箱（`.jsonl` 文件），通过发送消息通信。优势是：
- **进程隔离**：Agent 之间不共享任何状态，消除了竞争条件
- **持久性**：消息存储在文件系统，Agent 崩溃不会丢失
- **可调试性**：任何时刻都可以打开 `.jsonl` 文件查看通信历史
- **可扩展性**：未来可以替换为 Redis、RabbitMQ 等消息队列而无需改变接口

代价是性能——文件 I/O 比内存读写慢数个数量级。但对于 Agent 的场景（通信频率为"每秒数次"而非"每秒百万次"），文件 I/O 的性能完全够用。

**为什么用 JSONL 而非 JSON 数组？**

JSONL（每行一个 JSON 对象）的核心优势是**追加写入不需要读取整个文件**。当 Agent A 发送消息给 Agent B 时，只需要在 B 的收件箱文件末尾追加一行。如果用 JSON 数组，每次发送消息都需要读取整个数组、追加元素、写回整个数组——当收件箱有 1000 条消息时，这是 O(n) vs O(1) 的开销差异。

### 1.2 协议状态机的设计哲学

v7 的协议状态机解决的是**异步通信中的状态确认**问题。当 Lead 向 Teammate 发送 "shutdown_request" 后，Lead 需要知道 Teammate 是否确认了、是否同意了、如果没收到响应该怎么办。

`match_response()` 的**类型验证**是关键设计——`"shutdown_response"` 只能匹配 `"shutdown_request"` 类型的协议。这不是多余检查，而是**防御性设计**：如果 Teammate 的 shutdown_response 消息格式错误或类型不匹配，系统不会错误地将其解释为对 plan_approval_request 的响应。

这种设计防止了"状态污染"——一旦一个错误的消息被路由到错误的协议，整个系统的状态就变得不可信。

## 2. 系统架构

![v7 系统架构图](architecture.svg)

*Lead Agent 通过 MessageBus（文件邮箱）与多个 Teammate 线程通信，ProtocolState 管理结构化请求-响应（Shutdown + Plan Approval两种协议）。Teammate 运行在 idle loop 中持续待命。*

## 3. 文件结构

```
v7/
├── config.py        # 统一配置中心
├── llm.py           # Anthropic 客户端单例
├── log.py           # 统一日志
├── main.py          # 入口：agent_loop + 收件箱注入
├── tools.py         # 5 个核心工具
├── hook.py          # Hook 系统
├── skill.py         # 技能加载
├── subagent.py      # 子 Agent
├── compact.py       # 上下文压缩
├── memory.py        # 持久记忆
├── recovery.py      # 错误恢复
├── task_system.py   # 任务系统 + 后台执行
└── agent_teams.py   # 多 Agent 协作 ← 新增
```

## 4. 消息总线

### 4.1 设计理论

多 Agent 协作的核心问题是**跨线程通信**。每个 Teammate 运行在独立的 daemon 线程中，与 Lead 不在同一个执行上下文。消息总线的设计选择是**基于文件的邮箱系统**——每个 agent 有一个 `.jsonl` 收件箱文件。

文件邮箱 vs 内存队列的架构抉择已在概述 1.1 中详细分析。补充一点设计细节：v7 的 `read_inbox()` 采用"读取并删除"策略——消费式读取，每条消息只被处理一次。这在 Agent 协作场景中是一种务实的选择——大多数消息（如任务分配、状态报告）是一次性的。对于需要多次消费的消息（如系统通知），应该使用独立的"公告板"机制而非邮箱。

### 4.2 消息格式

```json
{
  "from": "alice",
  "to": "lead",
  "content": "Task completed: setup database",
  "type": "result",
  "ts": 1703001234.567,
  "metadata": {"request_id": "req_001234", "approve": true}
}
```

`type` 字段决定消息的处理方式：
- `message`：普通文本消息
- `result`：任务结果
- `shutdown_request` / `shutdown_response`：关闭协议
- `plan_approval_request` / `plan_approval_response`：计划审批协议

### 4.3 消费式读取

`read_inbox(agent)` 读取后立即删除文件——这是**消费语义**，确保消息不会被重复处理。真实 CC 使用 `proper-lockfile` 实现并发安全的消费式读取。

## 5. 协议状态机

### 5.1 设计理论

协议状态机解决的是**结构化请求-响应**问题。v7 的 MessageBus（简单消息传递）只能发送文本——"这是一条消息，内容如下"。但 Lead 和 Teammate 之间的交互远比"发一条消息"复杂——Lead 需要知道 Teammate 是否同意退出（shutdown），Teammate 需要知道 Lead 是否批准了它的计划（plan_approval）。

**协议的三层抽象**：
1. **关联层**：`request_id` 将请求和响应绑定——`shutdown_request("req_001")` 和 `shutdown_response("req_001")` 通过相同的 ID 关联
2. **类型层**：`type` 字段区分不同的协议——`match_response()` 验证响应类型与请求类型一致，防止协议混淆
3. **状态层**：`status` 追踪协议的进展——`pending → approved/rejected`，确保每个请求只被处理一次

**`match_response()` 的三重验证**：
1. 通过 `request_id` 查找原始请求——确保"这个响应对应哪个请求"
2. 检查 response_type 与 request.type 匹配——确保"shutdown_response 不能解决 plan_approval_request"
3. 检查 status 仍为 pending——确保"已处理过的请求不会被重复处理"

这种三重验证的设计是防御性编程的体现——假设任何环节都可能出错（消息延迟、重复发送、类型错误），在每个可能的错误点都有检查。

### 5.2 ProtocolState 数据结构

```python
@dataclass
class ProtocolState:
    request_id: str    # "req_004281" — 关联请求与响应
    type: str          # "shutdown" | "plan_approval"
    sender: str        # 发送方
    target: str        # 目标方
    status: str        # "pending" | "approved" | "rejected"
    payload: str       # 计划文本或关闭原因
    created_at: float  # 创建时间戳
```

### 5.3 match_response 函数

将响应关联到原始请求，带**类型验证**：

```
match_response(response_type, request_id, approve):
    ① 查找 pending_requests[request_id]
    ② 验证 response_type 与 request.type 匹配
       shutdown → 必须是 shutdown_response
       plan_approval → 必须是 plan_approval_response
    ③ 检查 status 是否仍为 pending（防止重复解决）
    ④ 更新 status = approved/rejected
```

类型验证防止了"用 shutdown_response 误批 plan_approval"的 bug。

### 5.4 两种协议

**Shutdown 协议（优雅关闭）：**
```
Lead                              Teammate
  │                                  │
  ├─ request_shutdown(teammate) ────→│
  │  BUS.send("shutdown_request")    │
  │                                  ├─ dispatch → 确认
  │←─────────────────────────────────┤
  │  BUS.send("shutdown_response")   │
  │                                  │
  ├─ match_response → approved       │  → 退出循环
```

**Plan Approval 协议（计划审批）：**
```
Teammate                           Lead
  │                                  │
  ├─ submit_plan(plan) ─────────────→│
  │  BUS.send("plan_approval_request")│
  │                                  ├─ 看到计划
  │                                  ├─ review_plan(req_id, approve)
  │←─────────────────────────────────┤
  │  BUS.send("plan_approval_response")│
  │                                  │
  ├─ dispatch → 注入审批结果         │
  │  "[Plan approved]" 或            │
  │  "[Plan rejected] feedback..."   │
```

### 5.5 统一收件箱消费

`consume_lead_inbox(route_protocol=True)` 是核心函数，同时服务于：
- `check_inbox` 工具（Lead 主动查看）
- REPL 循环（每轮自动检查）

路由逻辑：读取消息 → 遍历 → `*_response` 类型的消息通过 `match_response` 更新协议状态 → 返回所有消息。

这防止了"消息被消费但协议状态未更新"的 bug——如果 `check_inbox` 和 REPL 循环各自独立读取，可能会遗漏协议响应。

## 6. 队友 Idle Loop

### 6.1 设计理论

v7 的队友不使用固定轮次限制（如"最多 10 轮就退出"），而是进入 **idle loop**：LLM 轮次结束后，轮询收件箱等待新消息。这种设计更接近真实 CC 的行为——agent 在没有新任务时保持待命，而非立即退出。

idle loop 的行为：
```
LLM 轮次结束（stop_reason != tool_use）
    │
    ▼
idle loop (每 1 秒轮询):
    收件箱有消息?
    ├─ shutdown_request → 确认 → 退出
    ├─ plan_approval_response → 注入结果 → 回到 LLM
    └─ 普通消息 → 注入 → 回到 LLM
    收件箱为空?
    └─ 继续等待
```

### 6.2 协议消息分发

`_handle_inbox_message(name, msg, messages)` 按 type 分发：

| type | 处理 | 返回值 |
|---|---|---|
| `shutdown_request` | 发送确认响应 | True（停止循环） |
| `plan_approval_response` | 注入审批结果到消息历史 | False（继续） |
| 其他 | 忽略 | False（继续） |

## 7. Lead 工具清单

| 工具 | 功能 | 协议类型 |
|---|---|---|
| `spawn_teammate` | 启动队友 agent | — |
| `send_message` | 发送消息到队友 | — |
| `check_inbox` | 检查 Lead 收件箱 | — |
| `request_shutdown` | 请求队友关闭 | shutdown |
| `request_plan` | 要求提交计划 | — |
| `review_plan` | 审批/拒绝计划 | plan_approval |

## 8. 与 v6 的对比

| 维度 | v6 | v7 |
|---|---|---|
| Agent 数量 | 1 个主 Agent + 子 Agent | 1 个 Lead + 多个 Teammate |
| 通信 | 无（子 Agent 直接返回结果） | 消息总线（文件邮箱） |
| 协作协议 | 无 | shutdown + plan_approval |
| Teammate 生命周期 | 子 Agent 执行完即退出 | idle loop 持续待命 |
| 并行 | 子 Agent 串行 | Teammate 并行（独立线程） |
| 工具数 | 12 | 18（+6 团队工具） |
| 模块数 | 12 | 13 |

v7 的多Agent协作是 v8 自主Agent的基础——队友从被动等待消息变为主动认领任务。

### 9.1 收件箱消费的原子性问题

v7 的 `read_inbox()` 采用"读取并删除"策略——读取 `.jsonl` 文件后立即删除它。这是一种"消费语义"——每条消息只能被消费一次。但这引入了一个原子性问题：

如果 Agent 在"读取消息后、处理消息前"崩溃了——消息已被删除，但内容未被处理。消息永久丢失。这是一个经典的**消息队列可靠性问题**——"at-most-once" vs "at-least-once" 语义的权衡。

v7 选择了"at-most-once"（简单但可能丢消息），因为对于 Agent 协作场景：
- 丢失的消息可以被重现（通过重新发送）
- 重复处理的消息可能导致重复操作（如两次创建同一文件）

生产系统通常使用"at-least-once"配合幂等性（同一操作执行多次效果相同）来解决这个问题。

### 9.2 Leader-Follower 模式的扩展性上限

v7 的 Leader-Follower 模式中，Lead 是所有通信的枢纽。这种设计简单但有限制：
- Lead 的收件箱可能成为瓶颈（10 个 Teammate 同时发送消息）
- Lead 的上下文窗口需要包含所有 Teammate 的状态摘要（token 消耗）
- 如果 Lead 崩溃，整个系统失去协调能力

v8 通过自主 Agent 部分解决了这个问题——Teammate 不再完全依赖 Lead 分配任务，而是主动扫描看板。但根本问题（单点故障和中心化瓶颈）仍然存在。真正的分布式 Agent 系统应该使用去中心化的协调机制（如基于 DHT 的任务分配）。
