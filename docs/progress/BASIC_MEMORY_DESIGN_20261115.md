# 基础组记忆设计（方向三，设计文档；实现已完成，随 v5 一并落地）

## 问题

`mechanism_v4` 的记忆块只注入 enhanced 提示词。`build_messages('basic',
…, 'mechanism_v4', search_context=…)` 与 `build_messages('basic', …)`
逐字节相同（v4 计划文档已验证）——用 v4 跑 A/C 等于把 v3 对照重跑
一遍还贴上 v4 标签。因此 A/C 需要自己的记忆设计。

约束：基础提示词没有 Strategy 文本，生成器只回一个 python 代码块。
记忆不能是 Strategy 旁边的块，必须是提示词里的一句短指令。

## 设计：mechanism_v5b（basic_v2）

- **一句话记忆**：`SearchContext.render_compact()`，v2 语义压缩成一行，
  硬上限 40 词（实测约 17 词）。格式：
  `Search memory (this run only; search-set means, lower is better):
  refine {最好家族} ({最好均值}); avoid {饱和且差家族} ({次数}x,
  {均值}); …`——最多 2 个 avoid 项；refine 项永不删；超预算先删
  avoid 项。
- **插入位置**：basic 提示词的 `Return only the Python function in one
  ```python code block.` 之前（即父代 JSON 之后、返回指令之前）。
  basic_v1 文本是 v5b 文本去掉该行后的结果（测试保证）。
- **语义**：与 v5 相同（最好家族标“可精修”不避让；只避让饱和且
  差于当前最优的家族）。basic 生成器没有 Strategy 文本可供推理，
  所以记忆行只给两个可直接执行的事实（精修谁、避开谁），不给
  未试原型提示。
- **版本与方法记录**：`prompt_version("basic", "mechanism_v5b")` =
  `basic_v2`；`prompt_version("enhanced", "mechanism_v5b")` 报错
  （basic-only 版本）；方法 `search_context:
  mechanism_v5b_memory_v1`；`search_context.json` 照常写
  （`memory_version: v2`）。
- **附带修复**（与 v5 同批）：basic + v4/v5 带记忆调用现在是明确
  报错而非静默丢弃——v4 的静默丢弃陷阱（basic + v4 看似记忆运行，
  实为 basic_v1）不再可能。

## 实现（已完成，随 v5 一并实现与测试）

- `funpack/search_context.py`：`render_compact(max_words=40)`。
- `funpack/prompts.py`：`mechanism_v5b` 加入 `ENHANCED_VERSIONS`；
  `memory_style()`（basic + v5b → "compact"）与
  `search_context_label()` 为 search.py / protocol.py 的单一推导源。
- `funpack/search.py`：按 mode + 版本决定注入方式（full/compact/无），
  basic + v5b 时调 `render_compact()`。
- `funpack/protocol.py`：`expected_method` 改用同一推导。
- 测试：`tests/test_search_context.py` 24 个全过（含 v5b 提示词、
  compact 渲染、A 组 2 次离线集成：方法 basic_v2 /
  mechanism_v5b_memory_v1、字数 ≤40；basic + v5 集成：方法 off、
  无 search_context.json）。受影响模块 108 个全过；全套测试见
  `/tmp/full_suite_v5.log`。

## v3 校准基线（同一分类器，seed 11，实测）

| 组 | 候选（已评估） | 家族数 | 头号家族 | search 最好 | 选中 validation |
|---|---|---|---|---|---|
| A | 32 | 26 | windows[8,22];[30,45] 4/32 (12.5%) | 265.100 | 267.7 |
| C | 23（9 跳过） | 9 | mod 10/23 (43.5%) | 265.200 | 267.6 |

注意 A 组 v3 已然很多样（26 家族、头号 12.5%），C 组则集中
（9 家族、头号 43.5%）。方向三的校准标准需按组设定（见校准计划，
方向二完成后写），不能照搬 B 组的统一阈值。

## 校准计划（方向二完成后执行）

- A 组与 C 组 seed 11 各 32 次，mechanism_v5b，disable_on_mismatch_v1，
  islands 4，reset 50，HandshakeProvider，0 CNY。
- 输出目录：`run_muse_ctxv5b_A_seed11/`、`run_muse_ctxv5b_C_seed11/`；
  握手目录：`handshake-ctxv5b/`。
- 标准（预告，正式写进校准计划文档后锁定）：多样性按组设阈
  （A：家族数 ≥26 且头号 ≤12.5%，守住 v3 水平；C：家族数 ≥11
  且头号 <50%）；质量 A ≤265.067（优于 v3 的 265.100）、
  C ≤265.100（优于 v3 的 265.200）；无退化 0/0/≤2；次要 validation
  A ≤267.6、C ≤267.6。
