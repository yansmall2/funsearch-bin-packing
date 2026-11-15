# 上下文 v5 校准计划（跑前写定，跑后填结果）

## 动机：v4 的教训

v4 校准（B + D，64 次生成）的结论是：多样性大幅提高（B 组头号家族
50%→15.6%，家族数 11→18），但分数反而变差（B 265.267 vs v3 校准
265.133；D 一个候选也没赢过 BF）。关键观察：**64 次生成里一次也没
试过甜带家族**（家族表里无 `windows[8,22]`）。v4 把“饱和”等同于
“不要再试”，没有区分“饱和且最好”（值得精修）与“饱和且平庸”
（应该避开）——记忆块把搜索推离了已知最好的区域。

## v5 的修复（语义改动，不是堆更多历史）

`mechanism_v5` = v4 的提示词文本 + v2 记忆语义（`SearchContext`
新增 `memory_version` 参数；v1 即 v4 语义，默认不变）：

- 最好家族标注为 **“可精修”**（`Refine the best family: …; vary its
  constants or combine it with another idea.`），**永不进入避让清单**，
  即使它已饱和。
- 避让清单只列**饱和（≥3 次）且差于当前最优**的家族（严格差于，
  打平不算差）。
- 其余不变：家族定义（结构签名，常数不算新家族）、120 词硬预算、
  未试原型最多 2 行、只属于本次运行、只含 search 集均值。
- 方法记录：`search_context: mechanism_v5_memory_v1`。
- 附带修复：basic + v4/v5 带记忆调用现在是明确报错而非静默丢弃
  （v4 的静默丢弃陷阱：basic + v4 看似记忆运行，实为 basic_v1）；
  basic 臂的记忆是方向三的 `mechanism_v5b`，不在本次校准内。

实现文件：`funpack/search_context.py`（`memory_version`、
`_render_v2`、`render_compact`）、`funpack/prompts.py`
（`mechanism_v5`/`mechanism_v5b`、`memory_style`、
`search_context_label` 单一推导源）、`funpack/search.py`（按
mode + 版本决定是否注入记忆）、`funpack/protocol.py`
（`expected_method` 改用同一推导，与 search.py 一致）。
测试：`tests/test_search_context.py` 24 个（11 个 v4 原有全过 +
13 个 v5/v5b 新增）；受影响模块 108 个全过；全套测试后台跑，
以 `/tmp/full_suite_v5.log` 为准，冻结前（本次校准不走协议，
仅作完整性检查）确认全绿。

## 校准设计

- 组：B（enhanced），seed 11，32 次调用；islands 4，reset 50（32 次内
  不触发，与 v4 校准一致），audit 0.1（B 组无筛查，不适用），
  mechanism_v5，disable_on_mismatch_v1；HandshakeProvider，0 CNY。
- 输出目录：`run_muse_ctxv5_B_seed11/`；握手目录：`handshake-ctxv5/`；
  握手配置：`configs/muse_handshake_ctxv5.json`（max_requests 128，
  含一次完整重跑的余量）。
- 对照（同一分类器、同一 seed、同一调用数）：
  - v3 校准 B seed 11：best 265.133，32 次评估，11 个家族，头号家族
    16/32（50%），0 重复，0 格式违规，选中 validation 267.7。
  - v4 B seed 11：best 265.267，32 次评估，18 个家族，头号家族 5/32
    （15.6%），0 重复，0 格式违规，选中 validation 267.7；**甜带家族
    0 次尝试**。

## 跑前写定的成功标准（与 v4 计划相同，单种子 32 次，只看方向性信号）

1. 多样性：头号家族占比 < 50%，且不同家族数 ≥ 11。
2. 质量：search 最好 ≤ 265.100（优于 v3 校准的 265.133）。
3. 无退化：provider 错误 0，格式违规 0，完全重复 ≤ 2。
4. 次要：选中 validation ≤ 267.6（优于 v3 校准的 267.7）——单种子，仅作参考。

v5 的方向性假设（标准之外的观察项，不计入通过/失败）：甜带家族
（`windows[8,22]`）重新出现在家族表里，且最好家族的尝试次数
在运行后段继续增长（精修而非抛弃）。若 1 成立、甜带回归，但 2
仍不成立，说明“精修最好家族”还不足以把多样性转化为分数，需
考虑更长的校准或更强的精修引导；若甜带仍未出现，说明记忆块的
措辞仍在把搜索推离最好区域，v5 语义未生效。

若 1 不成立，说明记忆块没能改变生成器的探索分布；若 1 成立但 2
不成立，说明多样性提高了但没转化为更好的分数。两种情况都如实
写进报告。

## 结果（B 组 seed 11，32 次，mechanism_v5，已完成，2026-11-16 凌晨）

- 运行：`run_muse_ctxv5_B_seed11/`，termination `request_budget_completed`，
  32/32 请求，0 provider 错误，0 格式违规，0 完全重复，32 次有效评估。
  方法 `mechanism_v5 / search_context mechanism_v5_memory_v1`，
  `search_context_max_words` 最大 51 词（预算 120，从未触顶）。
  全套测试 228 个全绿（507 秒）。
- 家族分布（`classify`，32 个候选）：**18 个家族**，头号家族 `dist`
  4/32（12.5%），其次 `worst_fit` 3、`count_gate+dist+worst_fit` 3。
- search 最好：**265.267**（candidate_000008，`dist+worst_fit`：
  `item*3 <= max(bins)` 时 worst-fit，否则 BF；candidate_000030，
  `worst_fit`，同为 265.267）。选中 validation：**267.7**（= BF）。
- **甜带家族仍未出现**：家族表里无 `windows[8,22]`——v4 B、v4 D、
  v5 B 三次记忆校准共 96 次生成，甜带 0 次尝试。

### 对照预注册标准

| 标准 | v3 校准 B seed11 | v4 B seed11 | v5 B seed11 | verdict |
|---|---|---|---|---|
| 1 多样性：头号家族 <50% 且家族数 ≥11 | 11 家族，头号 16/32 (50%) | 18 家族，头号 5/32 (15.6%) | 18 家族，头号 4/32 (12.5%) | **通过** |
| 2 search 最好 ≤265.100 | 265.133 | 265.267 | 265.267 | 不通过 |
| 3 错误 0、违规 0、重复 ≤2 | 0/0/0 | 0/0/0 | 0/0/0 | **通过** |
| 4（次要）选中 validation ≤267.6 | 267.7 | 267.7 | 267.7 | 不通过 |

### 方向性假设的判定

- 甜带回归：**不成立**。v5 语义没有把甜带带回来。
- 最好家族被精修而非抛弃：**成立（机制层面）**。最终家族表：
  `worst_fit` 3 次、最好 265.267，从未进入避让清单；
  `plain` 3 次、最好 265.300，饱和且差于最优，被正确避让；
  `dist` 4 次、最好 267.067，饱和且差，被避让。生成器汇报
  也证实全程按记忆精修最好家族（plain → dist+worst_fit →
  worst_fit）。但精修没有转化为分数：最好成绩与 v4 完全相同
  （265.267），只是家族不同。
- 根本原因（见校准报告）：记忆是**每轮内**的，只能精修本轮已
  观测到的家族；甜带在本轮从未被观测到，所以无从精修。发现
  的瓶颈不在精修，而在发现。
