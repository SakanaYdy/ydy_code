# Agent v2 — Hook 系统 + 技能加载

## 1. 概述

v2 的核心改进是引入 **Hook 生命周期系统**和**技能系统**。Hook 将权限控制、日志记录、输出监控等横切关注点从主循环中抽离，使 `agent_loop` 变为纯编排层。技能系统通过 Markdown + YAML frontmatter 动态扩展系统提示词。

这两个改进的理论基础是**关注点分离**（Separation of Concerns）：主循环只负责"调用 LLM → 分发工具 → 回传结果"的编排逻辑，所有"什么时候该拦截、什么时候该警告、什么时候该记录"都由 Hook 处理。

### 1.1 关注点分离的深层分析

v1 的主循环是"聚合型"设计——编排逻辑、权限检查、日志输出全部混在一起。这不是"代码写得不好"，而是**架构缺乏边界**。当不同职责的代码混在一起时，会出现三个问题：

**① 修改的涟漪效应**

在 v1 中，如果想让"日志格式从纯文本变为 JSON"，必须修改主循环代码。但主循环代码也包含编排逻辑和权限检查——修改一处可能影响其他完全无关的功能。这违反了**单一职责原则**（一个模块应该只有一个导致它变化的原因）。

**② 测试的不可分割性**

v1 的主循环无法对权限逻辑做独立测试——要测试权限，必须启动整个 Agent。v2 通过 Hook 将权限逻辑提取为独立的 `permission_hook` 函数，可以单独加载和测试。

**③ 能力的不可组合性**

在 v1 中，如果想同时启用"大输出警告"和"破坏性命令确认"，需要在主循环中插入两段代码。这两段代码的顺序、交互、异常处理都需要手动管理。v2 的 Hook 注册表让组合变得简单——注册两个 handler，按顺序执行，第一个非 None 结果终止链。

### 1.2 事件驱动架构在 Agent 中的应用

Hook 系统本质上是**事件驱动架构**的简化实现。事件驱动架构的核心前提是：**发布者不关心订阅者是谁**。主循环发布 `PreToolUse` 事件，但不关心 `permission_hook` 和 `log_hook` 的存在。这种设计带来了两个关键属性：

**松耦合**：新增 handler 不需要修改主循环代码。v4 的自动日志派发、v5 的记忆注入、v6 的后台分发都是通过注册新 handler 实现的——这些功能的添加没有修改主循环的任何一行编排逻辑。

**可中断性**：事件驱动通常只是"通知"，但 v2 的 Hook 扩展了这个概念——handler 可以返回非 None 值来阻止事件的后续处理。这是借鉴了 Web 框架中间件的"短路"概念（如 Express.js 的 `res.status(403).end()`）。这种设计让 Hook 从被动的观察者升级为主动的守卫。

## 2. 系统架构

![v2 系统架构图](architecture.svg)

*主循环 agent_loop 通过 Hook 生命周期（UserPromptSubmit → PreToolUse → PostToolUse → Stop）与横切关注点解耦，技能系统动态组装 system prompt。*

## 3. 文件结构

```
v2/
├── main.py            # 入口：agent_loop（纯编排，通过 Hook 交互）
├── tools.py           # 5 个工具实现（与 v1 相同）
├── hook.py            # Hook 注册表 + 触发逻辑 + 5 个 handler ← 新增
├── skill.py           # 技能扫描 + SKILL.md 解析 + 系统提示词构建 ← 新增
└── permisssion.py     # 遗留权限模块（已被 hook.py 中的 permission_hook 替代）
```

## 4. Hook 生命周期系统

### 4.1 设计理论

Hook 系统的设计灵感来自事件驱动架构和中间件模式。核心思想是：**主循环不关心"工具执行前后该做什么"，只关心"调用工具、获取结果"**。所有横切关注点通过 Hook 注入。

**为什么 Hook 是"正确"的抽象级别**：在很多系统中，横切关注点（权限、日志、监控）通过面向切面编程（AOP）实现——在编译时或运行时"织入"额外的代码。AOP 在 Java 生态中流行，但有一个根本问题——织入点不易调试。当权限检查失败时，你很难在调用栈中定位"权限代码是在哪里、何时、被谁注入的"。

Hook 避免了这个问题：`trigger_hook("PreToolUse", block)` 是显式的调用点——你可以清楚地看到 Hook 在何时被触发。同时 `HOOKS` 注册表是全局可检查的——你可以查看所有已注册的 handler。这种**显式隐式结合**（调用点显式、handler 注册隐式）是 Hook 模式的关键设计特征。

**Hook 的"控制权反转"**：在传统的面向过程设计中，主函数调用辅助函数——主函数拥有控制权。在 Hook 模式中，控制权被反转——主循环不知道 handler 会做什么，handler 甚至可能阻止主循环的执行流。这是**好莱坞原则**（"Don't call us, we'll call you"）的体现。框架定义了事件点和触发时机，用户提供的代码在这些点上被回调。

**真实 Claude Code 的 Hook 系统对比**：Claude Code 的 Hook 系统在以下几个方面比 v2 更丰富：
- **条件注册**：handler 可以声明"只在某些条件下触发"（如"只在 Python 项目中触发 code_review skill"）
- **优先级**：handler 有显式优先级而非注册顺序决定执行顺序
- **异步支持**：handler 可以是异步的，支持非阻塞执行（如日志 handler 异步写入）

但核心抽象是相同的：事件→注册表→有序回调。v2 实现的是这个核心抽象。

### 4.3 Hook 返回值语义

Hook 的返回值语义是 v2 的关键设计决策——它定义了 Hook 与主循环之间的**契约**。

**三层返回值语义的架构含义**：

```
返回 None   → 放行，继续执行         （"我对这个操作没有意见"）
返回 str    → 阻断，字符串成为结果     （"我反对这个操作，原因如下..."）
返回 str(Stop) → 强制继续，内容注入   （"不要退出，这里有新信息需要处理"）
```

这个设计借鉴了 Web 中间件的"短路"概念。Express.js 的中间件通过 `next()` 放行、`res.status(403).end()` 阻止请求。v2 的 Hook 通过返回值实现相同的语义——`None` = `next()`，`str` = `res.status(403).end()`。但 v2 扩展了这个概念——Stop 事件上的 `str` 返回意味着"不要退出"，这是 Web 中间件没有的语义。

**为什么返回值语义必须明确**：v2 的 `trigger_hook` 有一个微妙的 bug（在 v3 中修复）——它没有返回 handler 的值。这看似是"忘记 return"的小错误，实际上暴露了接口设计的深层问题——**调用方和实现方对"这个函数是做什么的"有不同理解**。v2 的作者（开发者）认为 `trigger_hook` 是"执行所有 handler"，调用方（主循环）认为 `trigger_hook` 是"检查工具是否应该被阻止"。这种理解偏差是接口缺乏明确契约的直接后果。

### 4.4 已注册的 Handler

五个 Handler 展示了 Hook 系统的三种使用模式：

| Handler | 模式 | 架构意义 |
|---|---|---|
| `permission_hook` | **阻断型** | 横向安全控制。在任何工具执行前检查权限，可阻断危险操作 |
| `log_hook` | **观察型** | 横向可观测性。纯观察无副作用，记录所有工具调用 |
| `large_output_hook` | **警告型** | 横向质量保证。识别异常情况（超大输出），告警但不阻断 |
| `context_inject_hook` | **注入型** | 横向上下文增强。在用户输入前注入额外信息 |
| `summary_hook` | **记录型** | 横向统计。在会话结束时汇总信息 |

**为什么这五种 handler 覆盖了主要的横切关注点模式**：阻断型（安全）、观察型（日志）、警告型（监控）、注入型（上下文）、记录型（统计）。这五种模式基本覆盖了 Agent 系统中所有"不应该在主循环中处理"的逻辑。后续版本添加的 handler 都可以归入这五种模式之一。

### 4.5 权限 Hook 的实现

`permission_hook` 继承了 v1 的 3 级管道逻辑，但通过 Hook 机制注册：

```
permission_hook(block):
    if block.name == "bash":
        command = block.input.get("command", "")
        if 命中 deny list → 返回 "BLOCKED: ..."
        if 命中 rule list → 交互确认 → 返回或 None
    if block.name in ("write_file", "edit_file"):
        if 路径在工作区外 → 交互确认
    return None  # 放行
```

与 v1 的关键区别：权限检查不再写在主循环里，而是作为 PreToolUse handler 被调用。主循环只看到 `trigger_hook("PreToolUse", block)` 的返回值。

## 5. 技能系统

### 5.1 设计理论

技能系统的设计目标是**让 Agent 的能力可扩展，而不需要修改代码**。通过 Markdown 文件定义技能，Agent 可以在运行时动态加载新的行为模式。

**选择 Markdown + YAML frontmatter 的深层原因**：这个选择体现了"配置即代码"哲学的轻量实现。Markdown 而非 JSON 或自定义 DSL，是因为人类可读性（技能定义本身就是文档）、结构化与自由格式的结合（YAML 元数据 + 自由格式正文）、版本控制友好（Markdown diff 可读）。真实的 Claude Code 同样使用 SKILL.md 格式定义技能，包含 prompts、scripts 和 references——v2 实现了核心概念（扫描→解析→注入），省略了脚本执行和资源引用。

### 5.2 技能目录结构

```
skill/
├── code_review/
│   └── SKILL.md        # YAML: name, description + Markdown: 审查规则
├── data_analysis/
│   └── SKILL.md        # YAML: name, description + Markdown: 分析模式
├── debug_helper/
│   └── SKILL.md        # YAML: name, description + Markdown: 调试流程
└── translate/
    └── SKILL.md        # YAML: name, description + Markdown: 翻译规则
```

### 5.3 加载流程

```
① _scan_skills()
   遍历 SKILLS_DIR 下所有子目录
   检查是否包含 SKILL.md

② _parse_frontmatter(text)
   分割 "---" 获取 YAML 部分
   解析 name, description 字段
   返回 (meta, body)

③ build_system()
   基础 prompt + 技能目录列表
   "Available skills: code_review, data_analysis, ..."
   注入到 system prompt 中
```

### 5.4 系统提示词组装

v2 的 system prompt 由 `build_system()` 动态组装：

```
基础 prompt（角色定义）
    +
"Available skills: code_review, data_analysis, debug_helper, translate"
    +
工作目录信息
```

LLM 看到技能列表后，可以根据用户需求选择调用哪个技能。技能的正文（Markdown 部分）在用户请求特定技能时注入。

## 6. 与 v1 的对比

| 维度 | v1 | v2 |
|---|---|---|
| 权限检查 | 内联在主循环 | PreToolUse Hook |
| 日志记录 | 无 | log_hook + summary_hook |
| 输出监控 | 无 | large_output_hook |
| 系统提示词 | 固定字符串 | 动态组装（含技能列表） |
| 可扩展性 | 修改主循环代码 | 注册新 Hook 或添加 SKILL.md |
| 主循环职责 | 编排 + 权限 + 日志 | 纯编排 |

v2 的核心贡献是建立了 Hook 机制，这是后续所有版本扩展的基础——v4 的自动日志派发、v5 的记忆注入、v6 的后台分发都基于 Hook 系统。

### 7.1 Hook 系统的局限性与演进方向

v2 的 Hook 设计打开了扩展的大门，但仍有两个未解决的问题：

**① 无自动触发能力**

v2 的 Hook 必须在代码中显式调用 `trigger_hook()`。如果某个 Hook 应该在"每次工具执行前后"触发，必须在每个调用点手动添加。v4 解决了这个问题——通过在 `trigger_hook` 内部自动派发 `OnToolStart` 和 `OnToolEnd`，工具日志在零代码修改的情况下自动生效。

这种演进反映了 Hook 系统的设计规律：**先提供显式调用能力验证可行性，再添加隐式自动触发降低使用成本**。

**② Handler 执行顺序的语义约定**

v2 的 handler 按注册顺序执行，但没有显式的优先级系统。`permission_hook` 必须先于 `log_hook` 注册，但这种顺序是约定而非强制。在大型系统中，依赖注册约定管理执行顺序会导致脆弱的隐式依赖。真实的 Claude Code 通过更复杂的 Hook 分类（PreToolUse 下分为 blocking/non-blocking 两个子列表）来解决这个问题。

### 7.2 技能系统的设计局限

v2 的技能系统通过 Markdown + YAML frontmatter 定义技能，但存在两个局限：

**技能选择完全由 LLM 决定**：system prompt 中列出了可用技能的名称和描述，LLM 根据用户输入判断是否需要激活某个技能。但如果没有明确请求特定技能，LLM 可能忽略相关技能的存在。真实的 Claude Code 通过 `/` 命令系统（如 `/review` 显式激活 code_review 技能）弥补了这一缺陷。

**技能内容静态注入**：技能的定义在启动时加载一次，运行期间不会变化。这意味着技能不能根据上下文动态调整——一个 code_review 技能对所有语言的代码使用相同的审查规则。后续版本可以通过"上下文感知的技能注入"让技能内容根据项目语言、框架等自适应调整。
