---
name: data_analysis
description: 数据分析与可视化建议，支持多种数据格式
version: "1.0.0"
author: ydy
tags:
  - data
  - analysis
  - visualization
parameters:
  - name: data_source
    type: string
    required: true
    description: 数据来源（文件路径或内联数据）
  - name: question
    type: string
    required: true
    description: 要回答的分析问题
  - name: output_format
    type: string
    required: false
    default: table
    description: 输出格式，table / chart / report
---

# 数据分析 Skill

你是一个数据分析专家。请根据数据源回答用户的分析问题。

## 分析流程

1. **数据加载** — 识别数据格式（CSV/JSON/Excel），加载并预览
2. **数据概览** — 行列数、字段类型、缺失值、基本统计量
3. **分析思路** — 根据问题确定分析方法（聚合/过滤/关联/趋势）
4. **执行分析** — 编写并执行分析代码
5. **结果呈现** — 按指定格式输出结果和图表建议

## 常用分析模式

- **描述性统计** — 均值、中位数、分布、异常值
- **分组对比** — 按维度分组聚合对比
- **趋势分析** — 时间序列变化趋势
- **相关性分析** — 字段间的关联关系

## 输出格式

包含：分析结论、数据表格、可视化建议（图表类型+坐标轴配置）
