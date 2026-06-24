# Agent v3 — 模块化 + 子 Agent

## 1. 概述

v3 实现真正的模块化分离，并引入**子 Agent 架构**。配置中心和 LLM 客户端独立为单独模块，消除重复初始化。子 Agent 允许主 Agent 将子任务委派给独立的 Agent 循环执行，实现任务分解。

v3 还修复了 v2 的关键缺陷：`trigger_hook` 不返回值导致权限 Hook 形同虚设。这个 bug 说明了一个重要的工程教训——**接口设计必须明确返回值语义**，否则调用方无法正确使用。

### 1.1 依赖反转与模块化设计的本质

v1/v2 的问题是"看似有多个文件，实际上是一个文件"。`main.py` 和 `tools.py` 各自初始化 LLM 客户端、各自定义配置——它们通过**复制粘贴**共享逻辑，而不是通过**导入**共享逻辑。这不是真正的模块化，而是"文件级别的代码重复"。

v3 的根本性变化是引入了**依赖方向**的概念。在 v2 中：

```
main.py → 创建自己的 Anthropic 客户端
tools.py → 创建自己的 Anthropic 客户端
         (两者各自为政)
```

在 v3 中：

```
.env → config.py → {main.py, tools.py, hook.py, ...}
         llm.py  → {main.py, tools.py, subagent.py, ...}
```

依赖方向从"横向复制"变为"纵向继承"——所有模块从同一个源头获取配置和客户端。这种变化看似微小，但彻底改变了系统的可维护性：修改一个环境变量，所有模块同时生效；修改客户端初始化逻辑，一处修改全局生效。

### 1.2 子 Agent 的认知模型

子 Agent 不是一个"函数调用"——它是一次**认知委派**。当主 Agent 调用 `spawn_subagent("分析 config.py 的架构问题")` 时，发生的事情不是"执行一个函数并等待返回值"，而是：

1. 创建一个新的 Agent 实例，拥有独立的 LLM 对话
2. 这个子 Agent 自主探索、推理、试错
3. 子 Agent 在完成分析后，将**工作总结**（而非原始工具输出）返回给主 Agent

这模拟了真实团队协作中的"委派"模式——你不需要告诉下属每一步怎么做，只需要说明目标和交付物。

**为什么子 Agent 需要独立循环而不是简单的工具？** 因为有些任务是"开放式"的——你不知道需要读哪些文件、需要运行哪些命令、需要分析多少轮。只有一个有自主推理能力的 Agent 才能处理这种不确定性。工具只能执行确定的单步操作。

**防止无限嵌套的架构决策**：子 Agent 不能派发子 Agent。这不是技术限制，而是**复杂性控制**——每增加一层嵌套，调试难度呈指数级增长。一个三层嵌套的 Agent（A→B→C）出错时，A 只能看到 B 返回的模糊错误信息，无法了解 C 的具体问题。

## 2. 系统架构

![v3 系统架构图](architecture.svg)

*模块化的依赖反转设计（config + llm 单例模式），子Agent通过延迟导入避免循环依赖，共享Hook系统。*

## 3. 文件结构

```
v3/
├── config.py      # 统一配置中心 ← 新增
├── llm.py         # Anthropic 客户端单例 ← 新增
├── main.py        # 入口：agent_loop + 工具注册
├── tools.py       # 5 个工具实现（从 config/llm 导入）
├── hook.py        # Hook 系统（4 点 + 5 handler）
├── skill.py       # 技能加载
└── subagent.py    # 子 Agent 派发 ← 新增
```

## 4. 模块化设计

### 4.1 设计理论

v1/v2 的问题是**紧耦合**：LLM 客户端在 main.py 和 tools.py 中各初始化一次，配置硬编码在各文件中。这违反了**单一事实源**（Single Source of Truth）原则——同一个配置项在多处定义，修改时容易遗漏。

v3 的解决方案是**依赖反转**：所有模块从 config.py 和 llm.py 导入配置和客户端，不再自行创建。这实现了：
- **集中管理**：修改 .env 文件即可改变所有配置
- **单例模式**：LLM 客户端全局唯一，避免重复初始化
- **可测试性**：可以轻松替换配置或客户端进行测试

**设计原则详解：**

**① 单一事实源 (Single Source of Truth)**

v1/v2 中，`MODEL_ID` 被硬编码在 main.py 和 tools.py 两处。如果一个开发者只修改了一处，就会出现"看起来改了，实际上没改"的 bug。v3 将所有配置集中在 `config.py`，通过 `.env` 文件统一管理：

```
修改前 (v2):                     修改后 (v3):
  main.py → MODEL = "claude-3"    .env → MODEL_ID=claude-3
  tools.py → MODEL = "claude-3"   config.py → MODEL = os.environ["MODEL_ID"]
                                   main.py → from config import MODEL
                                   tools.py → from config import MODEL
```

这不是简单的"移到文件顶部"——它改变了配置的权威来源。`.env` 成为唯一的事实源，代码只是消费者。

**② 单例模式在 LLM Agent 中的特殊性**

为什么不每次调用时创建新客户端？答案在于连接开销：
- Anthropic API 客户端的初始化涉及 TLS 握手、认证 token 加载
- 在高频调用场景（Agent 每秒多次 LLM 调用），重复初始化会显著增加延迟
- 单例确保所有模块共享同一个 TCP 连接池

同时，单例也意味着 `ANTHROPIC_BASE_URL` 可以在一个地方设置，全局生效。这对兼容非官方 API 端点（如 DeepSeek、本地 Ollama）至关重要。

**③ 依赖反转的实际效果**

```
v2 的依赖关系 (高耦合):               v3 的依赖关系 (依赖反转):
  main.py → 自己创建 config           main.py → from config import MODEL
  tools.py → 自己创建 config           tools.py → from config import MODEL
  hook.py → 自己创建 config            hook.py → from config import MODEL
  (每处都可能不一致)                   (统一来源，绝对一致)
```

依赖反转的核心不是"谁来创建对象"，而是"谁拥有配置的权威"。v3 将权威从各个模块移到了 config.py。

### 4.2 配置中心（config.py）

`config.py` 是 v3 架构的**数据层入口**。它的职责是将环境变量（来自 `.env` 文件）解析为类型安全的 Python 变量，供所有模块使用。

**配置加载的实现与原理**：
```python
WORKDIR = Path.cwd()
SKILLS_DIR = Path(os.environ.get("SKILLS_DIR", WORKDIR / "skill"))
MODEL = os.environ["MODEL_ID"]
MAX_TOKENS = int(os.environ.get("MAX_TOKENS", "8000"))
SUB_MAX_TOKENS = int(os.environ.get("SUB_MAX_TOKENS", "4000"))
SUB_MAX_TURNS = int(os.environ.get("SUB_MAX_TURNS", "30"))
```

`python-dotenv` 库在程序启动时将 `.env` 文件中的键值对加载到 `os.environ`，`config.py` 通过 `os.environ.get(key, default)` 读取。这里的核心设计决策是**类型转换发生在 config.py 层面而非使用层面**——`int(os.environ.get("MAX_TOKENS", "8000"))` 确保所有使用者拿到的都是 `int` 类型，而非需要各自做 `int()` 转换。

**`config.py` 作为"配置契约"**：所有模块通过 `from config import MODEL` 获取配置，不直接读环境变量。这意味着如果要改变配置来源（如从 `.env` 改为 YAML 文件或远程配置中心），只需修改 `config.py`——其他模块完全不受影响。这是门面模式的另一种应用。

**配置的层次结构**：`WORKDIR` 和 `SKILLS_DIR` 使用 `Path` 类型（路径对象），数值类配置使用 `int` 类型，字符串配置使用 `str` 类型。这个类型转换不是装饰——它使得 Python 的类型检查器能捕获配置类型错误，而 v2 中所有配置都是字符串，类型错误只能运行时发现。

### 4.3 LLM 客户端（llm.py）

`llm.py` 的职责极其简单但架构意义重大：**确保整个系统中只有一个 Anthropic 客户端实例**。

**单例的实现方式**：
```python
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
```

Python 的模块导入机制天然保证了单例——模块在第一次被 `import` 后缓存在 `sys.modules`，后续的 `import` 返回同一个模块对象。因此模块级别的 `client = Anthropic(...)` 在全局只会执行一次。

**为什么 `ANTHROPIC_BASE_URL` 是可选的**：这个设计考虑了不同 API 提供商。Anthropic 官方 SDK 默认连接 `api.anthropic.com`，但通过 `base_url` 可以指向兼容的 API 端点（如 DeepSeek、本地 Ollama 等）。`os.getenv` 而非 `os.environ` 确保此参数缺失时 SDK 使用默认值。

**单例模式的工程代价**：全局单例的一个缺点是**不可测试性**——所有测试共享同一个客户端，无法为不同测试配置不同的客户端。v3 接受这个代价是因为教学版本的测试需求简单。生产系统通常使用依赖注入容器而非全局单例来解决测试隔离问题。

### 4.4 循环依赖处理

Python 的模块系统不允许循环导入——如果 `subagent.py` 在顶层导入 `tools.py` 的 `TOOL_HANDLERS`，而 `tools.py` 又间接依赖 `subagent.py`，Python 解释器会抛出 `ImportError`。

**延迟导入的实现原理**：`from tools import TOOL_HANDLERS` 不写在模块顶层，而是写在 `spawn_subagent()` 函数内部。当函数被调用时，Python 检查 `sys.modules`——如果 `tools` 模块已在缓存中（因为它被 `main.py` 等模块导入过），直接返回缓存的对象。如果不在缓存中，才真正执行导入。

**为什么延迟导入在 v3 中是正确选择**：`spawn_subagent()` 在实际运行中不会被频繁调用（用户不会每秒钟派发几十个子 Agent），所以每次导入的微小开销（微秒级）完全可忽略。同时，延迟导入保持了模块依赖的清晰性——`subagent.py` 不需要与 `tools.py` 合并，各自保持独立职责。

## 5. 子 Agent 系统

### 5.1 设计理论

子 Agent 的设计动机是**任务分解**：复杂任务可以拆分为多个子任务，每个子任务由独立的 Agent 循环执行。这与人类团队协作的方式类似——项目经理（主 Agent）分配任务给开发者（子 Agent），开发者独立完成后汇报结果。

**子 Agent 的本质：**

子 Agent 不是一个函数调用——它是一个完整的、独立的 Agent 循环。这意味着：
- 子 Agent 有自己的消息历史，独立于主 Agent
- 子 Agent 可以多次调用 LLM，逐步完成任务
- 子 Agent 可以试错：grep 没找到就换 glob，bash 报错就调整命令
- 子 Agent 返回的是"工作总结"，不是原始的工具输出

这与传统编程中的"函数调用"根本不同。函数调用是确定性的——相同的输入产生相同的输出。子 Agent 调用是非确定性的——LLM 可能采取不同的路径来完成任务。

**何时使用子 Agent vs 直接执行工具：**

| 场景 | 使用什么 | 原因 |
|---|---|---|
| 读一个文件 | 直接 read_file | 单步操作，无需推理 |
| 搜索一个函数 | 直接 grep | 单步操作，无需推理 |
| "分析这个模块的性能问题" | 子 Agent | 需要多步推理、阅读多个文件、综合分析 |
| "重构认证逻辑" | 子 Agent | 复杂的、有明确子目标的任务 |
| "修复这个 CI 错误" | 子 Agent | 需要诊断、尝试、验证的迭代过程 |

**子 Agent 的"危险"——为什么限制这么严格：**

每个子 Agent 调用都会消耗 LLM token（可能 10000+），如果无限嵌套，花费将指数级增长。更重要的是，多层嵌套的 Agent 几乎不可能调试——当子 Agent 的子 Agent 出了错，主 Agent 只能看到一个模糊的错误信息。

这与实际工程管理类似：你不会让项目经理去管理一个项目，然后那个项目又有自己的项目经理。扁平结构比深层嵌套更可控。

子 Agent 与主 Agent 的关键区别：
- **受限工具集**：没有 `spawn_subagent`，防止无限嵌套
- **独立限制**：`SUB_MAX_TURNS`（30 轮）和 `SUB_MAX_TOKENS`（4000）独立于主 Agent
- **简化提示**：明确禁止嵌套派发，专注于完成分配的任务
- **共享 Hook**：权限检查、日志记录等 Hook 对子 Agent 同样生效

### 5.2 执行流程

`spawn_subagent(description)` 的实现分为五个阶段，每个阶段承担不同的职责：

**阶段一：创建隔离的消息列表**。子 Agent 的消息历史独立于主 Agent。初始消息是 `{"role": "user", "content": description}`——子 Agent 看到的"用户请求"就是主 Agent 的任务描述。这种隔离机制保证了子 Agent 的推理不会被主 Agent 的对话历史污染。

**阶段二：受限工具集循环**。子 Agent 的 while 循环与主 Agent 结构相同（LLM 调用 → 判断 stop_reason → 工具执行 → 回传），但工具集受限——只有 bash/read/write/edit/glob，没有 spawn_subagent。权限检查等 Hook 照常运行，因为子 Agent 共享主 Agent 的 Hook 系统。

**阶段三：轮次控制**。`SUB_MAX_TURNS`（默认 30 轮）限制子 Agent 的最大迭代次数。这个数字是基于经验——大多数子任务在 5-15 轮内完成，30 轮的上限给出了充足的探索空间但防止无限循环。每轮迭代都会消耗 LLM API 调用（约 1000-4000 token），30 轮的上限也隐式控制了成本。

**阶段四：终止条件判断**。子 Agent 有两种终止方式：LLM 返回 `stop_reason == "end_turn"`（正常完成）或达到 `SUB_MAX_TURNS` 限制（强制终止）。v3 没有区分"任务完成"和"任务失败"——两者都返回给主 Agent，让主 Agent 自己判断结果质量。

**阶段五：结果提取**。这是整个子 Agent 系统最容易出错的环节，详见 5.3。

### 5.3 结果提取策略

子 Agent 结束后，系统需要从消息历史中提取"对人类有用的总结"。这个看似简单的操作实际上有微妙的语义问题。

**三级回退策略的实现与原理**：

```
尝试 1: 最后一条消息的文本内容
    → 如果失败：LLM 的最后一条消息可能是纯 tool_use（没有文本），
              或者是 tool_result（工具的输出，不是 LLM 的总结）

尝试 2: 最后一条 role=="assistant" 的消息的文本
    → 如果失败：assistant 消息中全是 tool_use 块，没有任何文本

尝试 3: 返回默认错误信息 "[Sub-agent did not produce a text response]"
    → 兜底：告诉主 Agent "子 Agent 没有生成有效输出"
```

**为什么最后一条消息不可靠**：LLM 可能在最后一轮调用了工具（如 `read_file`），但还没有基于工具结果生成总结文本。此时消息历史的最后一条是 `user: tool_result`（工具的输出），不是 assistant 的总结。直接返回工具输出对主 Agent 没有帮助——主 Agent 需要的是子 Agent 的**判断和结论**，不是原始数据。

**为什么"最后一条 assistant 消息"也可能不可靠**：如果子 Agent 的全部消息都是 tool_use（LLM 反复调用工具但从未输出文本），则没有任何文本可提取。这是 LLM 陷入"工具循环"的信号——LLM 不断尝试但无法得出明确结论。

**这个策略的架构意义**：它体现了"防御性设计"——不假设 LLM 会产出预期格式的输出，而是在每个可能的失败点都有兜底方案。v3 的三级回退是后续 v5 错误恢复系统的前身。

### 5.4 工具集限制

子 Agent 的工具集由 `_sub_tools()` 函数生成。它不是从主 Agent 的工具集中筛选，而是**重新构造**——从零开始定义子 Agent 的工具列表，确保不包含危险工具。

**实现方式**：`_sub_tools()` 从 `TOOLS`（主 Agent 的完整工具列表）中过滤出子 Agent 安全的工具。过滤规则是显式的——只包含 bash/read_file/write_file/edit_file/glob，显式排除 spawn_subagent。

```
子 Agent 可用: bash / read_file / write_file / edit_file / glob
子 Agent 不可用: spawn_subagent（防止嵌套）
```

**为什么不是"在 spawn_subagent 中捕获递归"而是"从工具列表中移除"**：在 spawn_subagent 函数中检测"如果嵌套则拒绝"听起来简单，但实际上是危险的——LLM 会尝试调用 spawn_subagent，然后收到错误结果，然后可能再次尝试（浪费 token 和时间）。从工具列表中完全移除，意味着 LLM **根本不知道** spawn_subagent 的存在，自然不会尝试。

**三层防御的价值**：禁止嵌套的三层理由不是简单的重复，而是独立的防御层：
1. **防止无限递归**（正确性层）：数学保证——排除工具意味着不可能调用
2. **控制调试复杂度**（可维护性层）：扁平的调用链可追踪，层数越多越难定位错误
3. **保护 LLM 调用资源**（成本层）：每层嵌套消耗独立的 token，三层嵌套 = 三层费用

## 6. trigger_hook 返回值修复

### 6.1 v2 的 Bug

v2 的 `trigger_hook()` 不返回 handler 的返回值，导致权限 Hook 的阻断信息丢失：

```python
# v2 的 trigger_hook（有 bug）
def trigger_hook(name, *args):
    for handler in HOOKS[name]:
        handler(*args)  # 返回值被丢弃！
```

主循环无法知道权限 Hook 是否阻断了工具执行。

### 6.2 v3 的修复

```python
# v3 的 trigger_hook（修复）
def trigger_hook(name, *args):
    for handler in HOOKS[name]:
        result = handler(*args)
        if result is not None:
            return result  # 返回第一个非 None 结果
    return None
```

主循环现在可以正确处理阻断：

```python
blocked = trigger_hook("PreToolUse", block)
if blocked:
    results.append({"tool_use_id": block.id, "content": str(blocked)})
    continue  # 跳过工具执行
```

## 7. 与 v2 的对比

| 维度 | v2 | v3 |
|---|---|---|
| 配置 | 硬编码在各文件 | config.py 集中管理 |
| LLM 客户端 | 多处重复初始化 | llm.py 单例 |
| 子 Agent | 无 | spawn_subagent + 受限工具集 |
| trigger_hook | 不返回值（bug） | 返回第一个非 None 结果 |
| 循环依赖 | 无（模块少） | 延迟导入解决 |
| 模块数 | 5 | 7 |

v3 的核心贡献在于建立了可扩展的模块化基础，子 Agent 则是多 Agent 协作的第一步。

### 8.1 依赖反转的实际效果验证

将 v3 与 v2 做 A/B 对比，最能体现依赖反转的价值：

**场景：切换 LLM 模型**

```
v2: 修改 main.py 中的 MODEL → 重启 → tools.py 仍使用旧模型 → 行为不一致
v3: 修改 .env 中的 MODEL_ID → 重启 → 所有模块使用新模型 → 行为一致
```

这不是"好用"和"不好用"的区别，而是"正确"和"不正确"的区别。v2 在切换模型时可能出现**状态分裂**——部分模块使用新模型、部分模块使用旧模型，导致不可预测的行为。

**场景：添加环境变量**

```
v2: 在 main.py 中 os.environ.get("NEW_CONFIG")  → tools.py 不知道这个配置
v3: 在 config.py 中定义 NEW_CONFIG = os.environ.get("NEW_CONFIG") → 所有模块可用
```

v3 的 config.py 充当了**配置的接口契约**——任何模块不需要知道配置来自环境变量还是文件，只需要 `from config import NEW_CONFIG`。

### 8.2 子 Agent 的"失败模式"理论

子 Agent 可能失败，理解它的失败模式对正确使用至关重要：

**① 过度探索**：子 Agent 在不必要的情况下调用大量工具（"先读 20 个文件了解一下"），耗尽 `SUB_MAX_TURNS` 限制。这是 LLM 的"好奇心陷阱"——给它探索的自由，它可能过度使用。

**② 过早收敛**：子 Agent 在找到第一个看似合理的答案后就停止探索，忽略更好的方案。这是 LLM 的"锚定效应"在 Agent 中的体现。

**③ 上下文迷失**：子 Agent 在处理长任务时，可能"忘记"原始任务目标，开始处理子任务中出现的次要问题。

v3 对这些失败模式没有内置的防御机制——`SUB_MAX_TURNS` 只是兜底限制，不是智能判断。后续 v5 通过错误恢复部分解决，v8 通过自主 Agent 的工作循环限制更精细地控制。
