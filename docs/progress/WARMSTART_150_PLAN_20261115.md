# 热启动组合进化：预注册计划（2026-11-15，冻结前）

## 问题

正式实验（protocol 3eb1f24d…，1200 次）的 12 个优胜程序全部属于 sweet-band
家族，最好的 search 成绩 265.033。问题：**从这些已验证的好机制出发，让模型
在它们之间组合、变异、继续进化 150 次/组，能否进一步改进？**

这是 2026-11-15 21:55 用户要求的“多种算法结合进化”的实现。文献种子方向
已被证伪（见 `docs/progress/LITERATURE_SEEDS_20261115.md`）：WF 303.067 远差
于 BF；Harmonic Match 需跨物品记忆箱类别，DSL 无状态而无法表达；两个候选
种子分别与 WF、BF 等价。因此种子改用正式实验的优胜程序——多样且够好，
是当前 DSL 与数据分布下“多种算法结合”最有料的原料。

## 设计（冻结内容）

- **种子**：正式实验 12 个优胜程序的代码（`candidates-sealed.json` 只读提取，
  逐字存入 `funpack/programs.py` 的 `WARMSTART_SEEDS`），去重后 9 个不同代码
  （B 组优胜者在 3 个种子上完全相同，C 组在 2 个种子上相同；数据库本就按
  code_hash 去重，重复种子不增加信息），加上 FF/BF，共 **11 个种子**，
  进入全部 4 个岛屿。筛选组（C/D）种子同样注册进行为筛查。
- **提示词**：`mechanism_v3c` = mechanism_v3 文本逐字节不变 + 末尾追加一段
  “结合两个父代的机制”的邀请（`funpack/prompts.py` 的 COMBINATION_PARAGRAPH；
  v3 是 v3c 的严格前缀，测试守护）。基础组（A/C）不受影响（basic_v1）。
- **规模**：4 arms × 3 seeds × 150 calls = **1800 次生成**，0 元，Muse 模型
  （muse-spark-1.3，handshake 提供方）。
- **其余与正式实验相同**：seeds 11/22/33，islands 4，reset_every 50，
  audit_rate 0.1，database_temperature 1.0，collision_policy
  disable_on_mismatch_v1，evaluation/validation/final timeout 30s。
- **选择规则不变**：search 前 5 → validation 均值 → search 均值 → 代码长度 →
  hash（SELECTION_RULE）。heldout 只在 seal 后的最终 evaluate 跑一次。

## 种子与提示词的绑定方式（纪律说明）

种子集合由 `enhanced_prompt_version` 决定：`mechanism_v3c` → 11 个种子，
其他版本 → 仅 FF/BF。绑定写在 `funpack/search.py`，由冻结协议记录的
`enhanced_prompt_version` 固定；不改动 plan schema（旧协议仍可按旧字段校验），
`protocol.py` 的 `expected_method` 推导无需改动（v3c 的 search_context 为 off）。
`funpack/programs.py` 已在 CORE_FILES 内，种子代码受冻结哈希保护。

## 预注册评判标准（跑之前写好）

对照基线：正式实验每组 search 最优均值 A 265.111 / B 265.033 / C 265.067 /
D 265.089（12 轮总均值 265.075）；validation 入选均值 A/C 267.6、B/D 267.7；
heldout test 均值 A 266.200 / B 266.133 / C 266.067 / D 266.144；
OR3 均值 A 210.533 / B 209.950 / C 210.400 / D 209.850。

1. **完成**：1800/1800 请求，0 provider error，12 轮全部
   `request_budget_completed`，否则 seal 拒绝，研究无效。
2. **主要标准（search）**：12 轮 best search-set 均值的总均值 < 265.075
   （正式实验总均值），且至少一个 arm 的均值优于其正式对照。
3. **次要标准（validation）**：入选 validation 总均值 ≤ 267.65（正式总均值）。
4. **最终标准（heldout，一次性）**：每组 test 与 OR3 均值与正式对照组比较；
   只有 test 与 OR3 同时不差于对照，才算“改进”，单侧改进记为“混合”。
5. **机制标准**：优胜程序中至少一个不是任一种子的逐字复制（即出现了真正的
   组合或新机制），用 family 分类器（`funpack/search_context.py`）检查；
   若 12 个优胜者全部等于种子，结论为“150 次未产生新机制”。

## 诚实声明（写入最终报告）

- 这是**继续进化**，不是独立实验：起点就是正式实验的优胜者，成绩不能与
  “从头跑”直接比较；正式实验的结论不受影响。
- 生成器是 Muse（muse-spark-1.3），不是 DeepSeek；绝对数值不与 2026-11-06
  DeepSeek 试点比较；组内 A/B/C/D 比较有效（同一生成器）。
- 若结果不改进，如实报告负面结果，不追加计划外变体。

## 执行顺序

1. 全套测试全绿 → 2. 冻结 `protocol-warmstart-150.json` → 3. 启动 12 轮
   part-run（`run_warmstart_part_<ARM>_seed<SEED>/`，handshake 目录
   `formal-handshake-warmstart/`）+ 4 个生成器协调员 → 4. 合并
   （`scripts/merge_formal_study.py`，出 `run_warmstart/`）→ 5. seal
   （`candidates-sealed-warmstart.json`）→ 6. 一次性 evaluate
   （`final_evaluation_warmstart/`）→ 7. 报告
   `docs/progress/WARMSTART_150_20261115.md` → 8. 分步推送到分支。

冻结后绝不编辑 `funpack/*.py`、`experiment.py`、`formal.py`（源码哈希与
冻结协议不符会导致验证失败）。方向二、三的实现必须等方向一 seal +
evaluate 完成后。
