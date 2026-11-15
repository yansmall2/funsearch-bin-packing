# 上下文 v5 校准报告（2026-11-16 凌晨）

## 结论先行（负面结果，与 v4 同型）

v5 的语义修复**按设计生效了，但没有改变结果**。B 组 seed 11 × 32 次：
多样性通过（18 个家族，头号 12.5%），质量不通过（search 最好
265.267，与 v4 完全相同，差于 v3 校准的 265.133），选中 validation
267.7（= BF）。**甜带家族第三次 0 尝试**（v4 B、v4 D、v5 B 共 96 次
生成，家族表里从未出现 `windows[8,22]`）。记忆能精修已观测到的
最好家族，但不能发现从未观测到的家族——瓶颈在发现，不在精修。

## 改动

`mechanism_v5` = v4 提示词文本 + v2 记忆语义（`SearchContext`
新增 `memory_version`；v1 即 v4 语义，默认不变，v4 行为逐字节
不受影响）：

- 最好家族标为“可精修”（`Refine the best family: …; vary its
  constants or combine it with another idea.`），永不进入避让清单，
  即使它已饱和。
- 避让清单只列饱和（≥3 次）且严格差于当前最优的家族（打平不算差）。
- 其余不变：家族定义、120 词硬预算、未试原型最多 2 行、只属于本次
  运行、只含 search 集均值。方法 `search_context:
  mechanism_v5_memory_v1`。
- 附带修复：basic + v4/v5 带记忆调用现在是明确报错而非静默丢弃
  （v4 的静默丢弃陷阱）；basic 臂的记忆是方向三的 `mechanism_v5b`
 （单行记忆，`basic_v2`），实现与测试已随 v5 一并落地，见
 `docs/progress/BASIC_MEMORY_DESIGN_20261115.md`。

测试：`tests/test_search_context.py` 24 个全过（11 个 v4 原有 +
13 个 v5/v5b 新增：v2 渲染语义、打平不避让、未饱和不避让、预算、
compact 单行、v5 = v3 + 记忆块、v5b basic 集成、basic + v5 记 off）；
受影响模块 108 个全过；**全套 228 个全绿**（507 秒）。离线 1 次
冒烟验证方法记录与记忆块无误。

## 执行

- B 组 seed 11 × 32 次，islands 4，reset 50（未触发），
  mechanism_v5，disable_on_mismatch_v1，HandshakeProvider（muse-spark-1.3），
  0 元。输出 `run_muse_ctxv5_B_seed11/`，握手 `handshake-ctxv5/`，
  日志 `logs/ctxv5_B_seed11.log`。
- 生成器协调员逐条手写 32/32，无批量生成脚本（校验脚本只做
  格式/DSL 检查）；Strategy 66–76 词（硬上限 80，全过）；#30 的
  Strategy 为 76 词，超过协调员软目标 75 词 1 词，但在硬上限内，
  按“写完不覆盖”铁律未修改。无中断、无重启、无超时。

## 结果

- 运行：termination `request_budget_completed`，32/32 请求，
  0 provider 错误，0 格式违规，0 完全重复，32 次有效评估，
  `search_context_max_words` 最大 51 词。
- search 最好：**265.267**（candidate_000008，`dist+worst_fit`：
  `if item * 3 <= max(bins): return [r for r in bins]` 否则 BF；
  candidate_000030，`worst_fit`，同为 265.267）。
  选中 candidate_000008，validation **267.7**（= BF）。
- 家族分布（32 个候选）：**18 个家族**，头号 `dist` 4/32
  （12.5%），`worst_fit` 3、`count_gate+dist+worst_fit` 3，
  其余 2 次以下。`windows[8,22]` 0 次。

### 对照预注册标准（`CONTEXT_V5_PLAN_20261115.md`）

| 标准 | v3 校准 | v4 | v5 | verdict |
|---|---|---|---|---|
| 1 多样性：头号 <50% 且家族数 ≥11 | 11 家族，头号 50% | 18 家族，头号 15.6% | 18 家族，头号 12.5% | **通过** |
| 2 search 最好 ≤265.100 | 265.133 | 265.267 | 265.267 | 不通过 |
| 3 错误 0、违规 0、重复 ≤2 | 0/0/0 | 0/0/0 | 0/0/0 | **通过** |
| 4（次要）选中 validation ≤267.6 | 267.7 | 267.7 | 267.7 | 不通过 |

## 为什么 v5 没有带回甜带（机制分析）

v5 的语义修复在机制层面是生效的，最终家族表可以验证：

| 家族 | 尝试 | 最好 | 是否曾被避让 | v2 规则判定 |
|---|---|---|---|---|
| worst_fit | 3 | 265.267（最优） | 否 | 最优家族，永不避让 ✓ |
| dist+worst_fit | 2 | 265.267（打平最优） | 否 | 未饱和 ✓ |
| plain | 3 | 265.300 | 是 | 饱和且差于最优 ✓ |
| dist | 4 | 267.067 | 是 | 饱和且差 ✓ |
| count_gate+dist+worst_fit | 3 | 267.800 | 是 | 饱和且差 ✓ |

生成器也按记忆执行了精修链（plain → dist+worst_fit → worst_fit）。
但精修的对象始终是本轮已观测到的家族。记忆是**每轮内**的：
运行开始时家族表里只有种子（FF/BF 都是 `plain`），最好家族
就是 `plain`（265.300）；精修 `plain` 只能得到 BF 附近的变体
（worst-fit 回退、分布门控），得不到甜带。甜带在 v3 校准里是
生成器自己发现的（无记忆时，父代演化把它带了出来）；在 v4/v5
里，三任生成器协调员的机制清单都被记忆块锚定在“精修当前最优”
上，而当前最优从来不是甜带。记忆能放大已发现的好东西，不能
凭空发现没出现过的东西。

换句话说：v4 的问题诊断（“避让清单把搜索推离了最好区域”）只
说对了一半。v4 确实避让了最好家族（v4 B 轮里甜带家族从未被试过，
但注意：v4 的避让清单列的是已观测的饱和家族，甜带从未被观测，
所以 v4 也没有“主动避开”甜带——甜带只是从来没被生成器提出来）。
v5 修复后，甜带依然没被提出来。真正的瓶颈是**生成器的机制发现
能力**，不是记忆的避让语义。

## 最好候选

candidate_000008（search 265.267，validation 267.7）：

```python
def priority(item, bins):
    if item * 3 <= max(bins):
        return [r for r in bins]
    return [-(r - item) for r in bins]
```

物品尺寸门控的 worst-fit/BF 混合：当最松的箱子足够大
（`max(bins) >= 3*item`）时取最松箱，否则 BF。与 v4 最好的
candidate_000019（`ratio+worst_fit`，265.267）数值相同、家族
不同。两者都只比 BF 好 0.033 箱（一个实例上少用 1 个箱的
1/30），在噪声量级。

## 缺陷与偏差披露

1. #30 的 Strategy 76 词：超过协调员自设软目标 75 词 1 词，
   在 80 词硬上限内，`format_ok=True`，流水线记 0 格式违规；
   按“写完不覆盖”铁律未修改。
2. 单种子 32 次：结论只是方向性信号，与 v4 校准相同的局限。
3. v5 与 v4 的最好成绩数值相同（265.267）纯属巧合——家族不同
   （v4 是 `ratio+worst_fit`，v5 是 `dist+worst_fit` /
   `worst_fit`），不要误读为同一候选。

## 对期中报告的意义

- **可以写**：记忆语义修复（v5）是经过预注册、实现、测试、
  校准的完整阴性结果：把最好家族从避让清单中拿掉并标为可精修，
  多样性保持（18 家族、头号 12.5%），但分数没有改进（265.267，
  与 v4 相同）；甜带家族三次记忆校准 0 尝试。记忆的瓶颈在发现
  不在精修。
- **必须写清**：v5 与 v4 的结论相同——记忆改变了探索分布，没
  有转化为分数；不要把“语义修复生效”（机制层面可验证）写成
  “结果改进”。
- **不得写**：“上下文改进了结果”、“v5 优于 v4”（最好成绩相同，
  validation 相同）。

## 下一步（供明天定）

1. 方向三（基础组记忆，mechanism_v5b）正在进行：A/C 各 32 次。
   若基础组同样出现“多样性通过、分数不通过”，记忆方向在单种子
   32 次尺度上可判定为没有转化，值得停下来而不是继续加码。
2. 若要继续追分数，记忆不是杠杆：考虑跨轮记忆（把正式实验的
   sweet-band 家族作为先验写进记忆，而不是每轮从零开始）——但
   这改变了“记忆只属于本次运行”的红线，需要重新设计并预注册，
   且有“用旧结果指导新搜索”的诚实披露问题；或者接受 BF 附近
   就是天花板，把期中报告的重点放在已验证的阴性结果上。

## 证据清单

- 计划 `docs/progress/CONTEXT_V5_PLAN_20261115.md`（跑前写定，
  含预注册标准与结果）；本报告
  `docs/progress/CONTEXT_V5_CALIBRATION_20261115.md`。
- 代码 `funpack/search_context.py`、`funpack/prompts.py`、
  `funpack/search.py`、`funpack/protocol.py`；测试
  `tests/test_search_context.py`（24 个）；配置
  `configs/muse_handshake_ctxv5.json`。
- 运行 `run_muse_ctxv5_B_seed11/`（study.json、baselines.json、
  B_seed11/：evaluated.jsonl、events.jsonl、provider_responses.jsonl、
  final_info.json、run_manifest.json、search_context.json、selection.json）；
  握手 `handshake-ctxv5/B_seed11/`（requests/、responses/、
  generations.jsonl）；日志 `logs/ctxv5_B_seed11.log`。
- 推送清单待触发：`~/workspace/funsearch-branch-manifests/ctxv5-p1`
  （只备清单，不触发，等主代理明早安排）。
