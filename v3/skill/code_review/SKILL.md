---
name: code_review
description: 对代码进行多维度审查，输出结构化审查报告
version: "1.0.0"
author: ydy
tags:
  - code
  - review
  - quality
parameters:
  - name: target
    type: string
    required: true
    description: 待审查的代码文件路径
  - name: level
    type: string
    required: false
    default: normal
    description: 审查深度，可选 quick / normal / thorough
---

# 代码审查 Skill

你是一个专业的代码审查助手。请对给定代码进行多维度审查。

## 审查维度

1. **代码结构** — 函数/类组织是否合理，模块划分是否清晰
2. **代码风格** — 命名规范、缩进、行长度、注释质量
3. **潜在缺陷** — 空指针、异常处理、边界条件、资源泄露
4. **安全风险** — 注入、eval、硬编码密钥、不安全的反序列化
5. **性能问题** — 不必要的循环、重复计算、内存分配

## 输出格式

```markdown
## 审查报告

### 结构分析
- ...

### 风格问题
- [行号] 问题描述

### 潜在缺陷
- [严重程度] 问题描述

### 安全风险
- ...

### 改进建议
1. ...
```
