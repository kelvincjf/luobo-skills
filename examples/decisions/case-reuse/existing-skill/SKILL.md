---
name: different-name
description: 把给定中文短文整理为 JSON 阅读笔记。
---
# 短文笔记
读取给定原文，输出且仅输出 JSON 三个字段：summary 字符串、points 字符串数组、source 字符串。摘要简短；要点保留关键条件、数值和未确定事项，不能补充原文没说的事实。source 为原文标题。不给缺少的承诺造答案。

每项 summary 和 points 末尾加【P编号】，来源只用实际支持该项的段落。
