# 文献种子 + 多算法结合进化：调研与种子评估（2026-11-15）

## 用户要求（2026-11-15 21:55）

- 多次实验，每组 150 次。
- 先给出几种网上表现比较好的算法，让模型根据多种算法结合进行进化迭代。
- 往多个地方尝试，不只做一个地方。
- 这部分内容也要备份，跟上次同一个位置（分支 `fatbaby/smoke-test-20261114`），
  且后续重要项目都要备份。

## 结论先行（诚实版）

1. **文献里没有在均匀随机数据上比 BF 更好的在线算法。** BF 在均匀随机序列上的
   平均性能比是最优的（average-case ratio 1），期望浪费
   Θ(√n log^¾n)，文献原话是 “BF is almost the best online algorithm with
   respect to average performance”（Kamali & López-Ortiz 2014, arXiv:1404.4526）。
   文献中“更好”的算法好的是**最坏情况竞争比**，不是在均匀数据上的平均表现。
2. **可表达的文献种子没有一个接近 BF。** 实测（search 集，30 实例）：
   WF 303.067，BF 265.3；另两个候选种子经证明分别等价于 WF 和 BF，等于没有新料。
3. **因此不建议把 150 次 × 12 轮花在文献种子上。** 更有料的“多种算法结合进化”
   是**热启动**：以正式实验的 12 个优胜程序（不同家族、不同门控）+ FF/BF
   为种子，让模型在已验证的好机制之间组合、变异、继续进化。
   详见 §5 提议。

## 1. 文献调研（已验证来源）

来源：Kamali & López-Ortiz, “An All-Around Near-Optimal Solution for the
Classic Bin Packing Problem”, arXiv:1404.4526（全文已读 Table 1 与 §2）；
Csirik et al., “Sum-of-Squares Heuristics for Bin Packing”, JEA 2001
（实验结论部分已读）。

| 算法 | 平均性能比（均匀随机） | 竞争比（最坏情况） | 备注 |
|---|---|---|---|
| Next Fit (NF) | 1.33 | 2 | 只用最新开的箱 |
| First Fit (FF) | 1（最优） | 1.7 | 已是种子 |
| Best Fit (BF) | 1（最优） | 1.7 | 已是种子，期望浪费最小 |
| Harmonic (HA, Lee & Lee) | ~1.27–1.29 | →1.691 | 按尺寸分箱；平均很差 |
| Refined Harmonic | ~1.28 | 1.636 | 平均很差 |
| Harmonic++ / Modified Harmonic | >1（1.18–1.28） | 1.588–1.615 | 平均比 BF 差 |
| Harmonic Match (HM) | **1（与 BF 相当）** | →1.691 | 平均与最坏兼得，但见 §2 |
| Refined Harmonic Match (RHM) | 1 | <1.636 | 同上 |
| Sum of Squares (SS) | 与 BF 相当，无渐近优势 | — | 低随机性数据上略好，高随机性上 BF 更好 |

关键引文（1404.4526 §1）：“All algorithms that do better than First Fit
in the worst-case seem to do much worse in the average case.” HM/RHM 是例外，
但代价见 §2。

## 2. DSL 可表达性分析（决定性约束）

候选程序必须是 `def priority(item, bins)`，无状态：每次调用只看到当前物品
`item` 和可行箱的**剩余容量列表**（按开箱顺序），看不到箱子历史、箱内物品、
箱子类别。`funpack/programs.py` 的 `_SAFE_NAMES` 只有
abs/min/max/sum/len/round/sqrt/log/exp；没有 `range`、`enumerate`，推导式里
拿不到下标；语句只允许赋值/返回/if。

| 算法 | 能否表达为 priority(item, bins) | 原因 |
|---|---|---|
| FF / BF / WF | 能 | 只依赖剩余容量 |
| NF | **不能** | 需要“最新开的箱”，推导式里无法定位最后一个元素 |
| Harmonic / Harmonic Match / IFF / OM / MBF | **不能（精确版）** | 需要记住每个箱属于哪个尺寸类别（跨物品的状态）；HM 的配对思想可近似，见 §3 |
| Sum of Squares | 原则上能（依赖全部剩余容量分布），但规则复杂，种子价值低 | 文献实验结论：对均匀数据无渐近优势 |

## 3. 种子评估（实测）

脚本：`eval_literature_seeds.py`（直接调用 `funpack.packing.evaluate` 与
`funpack.programs.Program`，种子代码均通过 DSL 校验）。
数据：`data/search.json` 30 实例（选种依据），`data/validation.json` 10 实例
（仅描述性参考，不用于选种）。

| 种子 | search 均值 | validation 均值 | 结论 |
|---|---|---|---|
| seed_ff（已有） | 269.500 | 272.400 | — |
| seed_bf（已有） | 265.300 | 267.700 | — |
| seed_wf | 303.067 | 304.200 | 远差于 BF；父代采样按分数加权，基本不会被选为父代，是死种子 |
| seed_hybrid_bf_wf（大件→BF，小件→WF） | 303.067 | 304.200 | **与 WF 完全等价**，见证明 |
| seed_hm_inspired（小件优先配大件箱） | 265.300 | 267.700 | **与 BF 完全等价**，见证明 |

### 证明 1：hybrid ≡ WF

30 个 search 实例中，大件（>50）共 297 个，在 WF 动态下**全部只有 1 个可行箱**
（100% 被迫选择）。因此 hybrid 的 BF 分支从不起作用，小件分支与 WF 相同，
逐实例箱数与 WF 完全一致。DSL 本身无问题（探针验证：item=60 时返回 BF 分数，
item=30 时返回 WF 分数）。

### 证明 2：hm_inspired ≡ BF

大件时两者都是 BF。小件时：若存在可行箱 r ≤ 50，BF 的 argmin（最小可行剩余）
必落在该子集内，hm_inspired 在该子集内同样按 BF 选 → 相同；若不存在，
两者都在全部可行箱上按 BF 选 → 相同。故对任何输入行为一致。

## 4. 对“多种算法结合”的重新理解

FunSearch 的“结合”发生在父代采样：生成器看到两个父代程序，可以融合、变异
它们的机制。结合要有效，父代必须**多样且够好**。文献种子的问题不是“不够有名”，
而是：好的表达不了（HM/NF），能表达的要么很差（WF），要么等价于已有种子。

真正多样且够好的父代已经存在：**正式实验的 12 个优胜程序**。它们分属不同家族
（sweet-band 变体、ratio 门控、奇偶门控等），search 最好成绩 265.033–265.167，
全部在 heldout 上击败 BF。以它们为种子，是“多种算法结合进化”在当前 DSL 与
数据分布下最有料的实现。

## 5. 提议：多方向实验计划（待用户确认）

### 方向一（主实验）：热启动组合进化，150 次/组

- 种子：12 个正式优胜程序 + FF/BF，共 14 个种子，进入全部 4 个岛屿。
- 提示词：mechanism_v3 + 明确邀请“结合两个父代的机制”（措辞变更需记入新协议）。
- 规模：4 arms × 3 seeds × 150 calls = 1800 次生成，0 元，Muse 模型。
- 协议：新冻结协议（新协议号），旧密封实验（3eb1f24d…）不受影响；
  选择规则仍只用 search 集，heldout 绝不参与调参。
- 预期：从 265.0 起步继续进化，而不是从 265.3 从头来；回答“已验证的好机制
  之间能否组合出更好的”。

### 方向二：记忆语义修复标定（v5，接昨晚 v4 的教训）

- 改动：最优家族标注为“可精炼”而非回避；只回避“饱和（≥3 次）且差于当前最优”
  的家族。新提示词版本 mechanism_v5（需代码 + 测试）。
- 标定：B 组 seed 11 × 32 次，对照 v4/v3 的预注册标准；通过后再考虑进正式规模。

### 方向三：基础组记忆设计（A/C 组）

- v4 的记忆块只注入增强提示词，基础组 v4 与 v1 逐字节相同（已验证）。
  A/C 需要专门的基础提示词记忆设计（新版本工作），然后标定。

### 不做的方向

- 文献种子 150 次正式实验：种子实测无新料，WF 是死种子，HM 表达不了。
  调研结论写入期中报告的“相关工作 / 局限”即可。

## 6. 时间估计

- 方向一：每轮 150 次 ≈ 70–90 分钟（12 轮并行），今晚批准即可启动，明早出结果。
- 方向二：约 20 分钟，可与方向一并行。
- 方向三：设计 + 实现 + 标定，约 1–2 小时工作量，排在方向二之后。

## 证据清单

- `eval_literature_seeds.py`（种子评估脚本，search/validation 实测）
- Kamali & López-Ortiz 2014, arXiv:1404.4526（Table 1、§1、§2 已读）
- Csirik et al. 2001, Sum-of-Squares JEA（实验结论：对均匀数据无渐近优势）
- `funpack/programs.py`（DSL 限制：`_SAFE_NAMES`、语句类型）
- `funpack/packing.py:pack`（bins 按开箱顺序；大件被迫选择 297/297 的测量）
