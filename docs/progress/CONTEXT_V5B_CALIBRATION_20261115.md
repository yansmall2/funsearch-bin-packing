# 基础组记忆 v5b 校准报告（2026-11-16 凌晨）

## 结论先行（负面结果；A 组出现严重集中）

单行记忆在机制上正常工作（方法记录正确、字数合规、生成器按
记忆执行），但**没有转化为分数**，且在 A 组造成了严重集中。
两组 8 项预注册标准中只通过 2 项（两组的“无退化”），其余
6 项全部不通过。五次记忆校准（v4 B、v4 D、v5 B、v5b C、v5b A）
共 160 次生成，**甜带家族 0 次尝试**，记忆方向在单种子 32 次
尺度上没有一次把多样性转化为更好的分数。

## 设计与实现（见 `BASIC_MEMORY_DESIGN_20261115.md`）

`mechanism_v5b` = basic_v1 + 单行记忆（`render_compact`，v2 语义，
硬上限 40 词），插在返回指令之前；`prompt_version` 为 `basic_v2`；
方法 `search_context: mechanism_v5b_memory_v1`。实现与测试已随
v5 一并落地（`tests/test_search_context.py` 24 个全过；全套
228 个全绿）。离线 1 次冒烟验证方法记录与记忆行无误（13 词）。

## 执行

- A 组与 C 组 seed 11 各 32 次，mechanism_v5b，
  disable_on_mismatch_v1，islands 4，reset 50（未触发），
  HandshakeProvider（muse-spark-1.3），0 元。
- 输出：`run_muse_ctxv5b_A_seed11_r2/`、`run_muse_ctxv5b_C_seed11/`；
  握手：`handshake-ctxv5b-r2/`、`handshake-ctxv5b/`；日志
  `logs/ctxv5b_{A_seed11_r2,C_seed11}.log`。
- **A 组 r1 中断**：28/32 处运行时重启杀掉实验进程
  （`CONTEXT_V5B_A_R1_INTERRUPTED_20261115.md`），r2 全新重跑；
  r1 部分证据保留（27 次已评估，最好 265.133），不并入判定。
- 生成器协调员均为逐条手写，无批量生成脚本；C 组 32/32、
  A 组 r2 32/32 均通过格式/DSL 校验；#25（A r2）的平台行为
  见“缺陷披露”。

## 结果

### A 组（r2）

- 运行：termination `request_budget_completed`，32/32 请求，
  0 provider 错误，0 格式违规，0 完全重复，32 次有效评估，
  `search_context_max_words` 最大 21 词。
- search 最好：**265.167**（candidate_000005，
  `scaled+triangular`：缩放 BF + V 形罚 + 饱和回扣）。
  选中 validation：**267.7**（= BF）。
- 家族分布（32 个候选）：**5 个家族**，头号
  `scaled+triangular` **24/32（75.0%）**，其次 `scaled` 3、
  `dist+scaled+triangular` 3。

#### 对照预注册标准（A 组）

| 标准 | v3 校准 A | v5b A r2 | verdict |
|---|---|---|---|
| 1 多样性：家族数 ≥26 且头号 ≤12.5% | 26 家族，头号 12.5% | 5 家族，头号 75.0% | 不通过 |
| 2 search 最好 ≤265.067 | 265.100 | 265.167 | 不通过 |
| 3 错误 0、违规 0、重复 ≤2 | 0/0/0 | 0/0/0 | **通过** |
| 4（次要）选中 validation ≤267.6 | 267.7 | 267.7 | 不通过 |

### C 组

- 运行：termination `request_budget_completed`，32/32 请求，
  0 provider 错误，0 格式违规，0 完全重复；23 次有效评估，
  9 次行为跳过，2 次审计（1 次 count 不符、1 次 trace 不符），
  `search_context_max_words` 最大 21 词。
- search 最好：**265.133**（candidate_000031，分类器标为
  `plain`，实为比率带惩罚）。选中 validation：**267.7**（= BF）。
- 家族分布（23 个已评估候选）：**8 个家族**，头号 `plain`
  10/23（43.5%），其次 `triangular` 3、`ratio` 3。

#### 对照预注册标准（C 组）

| 标准 | v3 校准 C | v5b C | verdict |
|---|---|---|---|
| 1 多样性：家族数 ≥11 且头号 <50% | 9 家族，头号 43.5% | 8 家族，头号 43.5% | 不通过 |
| 2 search 最好 ≤265.100 | 265.200 | 265.133 | 不通过（但优于 v3） |
| 3 错误 0、违规 0、重复 ≤2 | 0/0/0 | 0/0/0 | **通过** |
| 4（次要）选中 validation ≤267.6 | 267.6 | 267.7 | 不通过 |

## 为什么 A 组严重集中（机制分析）

v5 的“精修最好家族”语义在 basic 模式下被执行得过于彻底：

1. 运行早期一个 `scaled+triangular` 候选拿到 265.167 成为最好
   家族，记忆行全程说“精修它”，生成器照做 24 次（V 形罚的
   幅度扫描、中心移动、折扣、对称翻转等变体）。basic 生成器
   没有 Strategy 文本可供推理替代方案，单行记忆成了主要指导。
2. 避让清单未能阻止：`scaled`（3 次，265.267）与
   `dist+scaled+triangular`（3 次，265.267）虽被避让，但
   `scaled+triangular` 是另一个精确签名，不在避让之列。
   家族按精确签名定义，近亲家族不受避让约束。
3. 对比：v4/v5 的 B 组（enhanced，有 Strategy 文本）记忆让多样性
   上升（11→18 家族）；C 组（basic + 筛查）记忆让多样性微降
   （9→8 家族）；A 组（basic，无筛查）记忆让多样性崩塌
   （26→5 家族）。记忆行的“精修”指令在信息最少的模式下
   影响最大。

C 组则相反方向温和集中：家族数 9→8，头号占比持平 43.5%，
只是头号家族从 `mod` 换成 `plain`。C 组的最好成绩 265.133
优于 v3 的 265.200，但未达到预注册的 265.100。

## 测量说明（分类器的盲区）

C 组最好候选 #31 的比率带思想是用 `(r-item)/r` 的除法表达的，
没有触发 `ratio` 标志（该标志要求残差与含 item 的表达式直接
比较），所以被归入兜底的 `plain` 家族。`plain` 10 次里包含了
结构上不同的机制，家族数 8 低估了真实的机制多样性。但预注册
标准写的是分类器口径，判定仍按分类器执行，此处仅披露测量局限。
A 组的 5 家族不存在此问题——24 次 `scaled+triangular` 是真实的
同家族反复精修（V 形罚变体）。

## 最好候选

A 组 candidate_000005（search 265.167，validation 267.7）：

```python
def priority(item, bins):
    return [-(r - item) / item - 1.0 * abs((r - item) / item - round((r - item) / item)) + 0.5 * ((r - item) // item) / ((r - item) // item + 1) for r in bins]
```

C 组 candidate_000031（search 265.133，validation 267.7）：

```python
def priority(item, bins):
    return [-(r - item) / r - (0.3 if 0.25 < (r - item) / r and (r - item) / r <= 0.5 else 0.0) for r in bins]
```

两者 validation 均为 267.7（= BF），search 的微弱优势没有泛化。

## 缺陷与偏差披露

1. A 组 r1 被运行时重启中断（28/32），r2 全新重跑；r1 部分轮
   最好 265.133 反而优于 r2 的 265.167，重启丢掉更好部分轮的
   损失如实记录；判定以 r2 完整 32 次为准。
2. A 组 r2 的 #25（强 V 幅度 1.0）在 q≤1 区间形成平台（得分恒
   −1），退化为“任一剩余≤1 item 的箱子等价、取最早者”的满足式
   机制；校验合法（format_ok、DSL 合法），行为特殊，特此说明。
3. A 组 r2 的 #6–#8 用了 `max`（属后来被避让的 dist 组合），
   写于避让行出现之前，不违规。
4. 单种子 32 次：结论只是方向性信号，与 v4/v5 校准相同的局限。
5. 三名生成器协调员（B v5、A r1、A r2、C）的汇报均与文件证据
   核对一致（请求数、机制清单、记忆遵循情况）。

## 对期中报告的意义

- **可以写**：基础组记忆（v5b）是经过预注册、实现、测试、
  校准的完整阴性结果：单行记忆在机制上正常工作，但两组均未
  改进分数（A 265.167 差于 v3 的 265.100；C 265.133 优于 v3
  的 265.200 但未达标准）；A 组还出现了记忆导致的严重集中
  （26→5 家族）。与 v4/v5 的 B 组结论互相印证：记忆改变探索
  分布，没有转化为分数。
- **必须写清**：五次记忆校准共 160 次生成，甜带家族 0 次尝试；
  记忆的瓶颈在发现不在精修，且在 basic 模式下“精修最好家族”
  可能反噬多样性。
- **不得写**：“上下文改进了结果”、“v5b 优于 v5/v4”（最好成绩
  与 validation 均未改进；A 组多样性反而崩塌）。

## 证据清单

- 计划 `docs/progress/CONTEXT_V5B_PLAN_20261115.md`（跑前写定，
  含按组预注册标准与结果）；设计
  `docs/progress/BASIC_MEMORY_DESIGN_20261115.md`；中断记录
  `docs/progress/CONTEXT_V5B_A_R1_INTERRUPTED_20261115.md`；
  本报告 `docs/progress/CONTEXT_V5B_CALIBRATION_20261115.md`。
- 运行 `run_muse_ctxv5b_A_seed11_r2/`、`run_muse_ctxv5b_C_seed11/`、
  `run_muse_ctxv5b_A_seed11/`（r1 部分轮，保留为证）；握手
  `handshake-ctxv5b-r2/A_seed11/`、`handshake-ctxv5b/A_seed11/`、
  `handshake-ctxv5b/C_seed11/`（各含 requests/、responses/、
  generations.jsonl）；日志 `logs/ctxv5b_A_seed11_r2.log`、
  `logs/ctxv5b_C_seed11.log`、`logs/ctxv5b_A_seed11.log`。
- 推送清单待触发：`~/workspace/funsearch-branch-manifests/ctxv5b-p1`
  （只备清单，不触发，等主代理明早安排）。
