# 数据与在线装箱接口

本层只使用 Python 标准库。数据生成、探针采样、程序评估相互独立。

## 先看手算例子

输入物品 `[6, 7, 3, 4]`、箱容量 `10`：

- First Fit（FF）依次选择第 0、1、0、2 个箱子，共 3 个；剩余 `[1, 3, 6]`。
- Best Fit（BF）依次选择第 0、1、1、0 个箱子，共 2 个；剩余 `[0, 0]`。

实现接收当前物品和**已打开且能容纳它的箱子**的剩余空间，按开箱顺序排列。
评分越大越优，分数相同时选择较早的箱子；只有没有可用箱子时才开新箱。
不排序物品，也不向评分函数提供未来物品。这个项目采用这一受限在线策略空间。

`pack(items, capacity, scorer, trace=False, state_callback=None)` 返回
`bins_used`、`remaining`；`trace=True` 时还返回逐物品 `assignments`（从 0 开始）。
`state_callback` 可收到 `{item, bins, step}`；仅在至少一个已开箱子可用时触发。

`evaluate(scorer, instances)` 返回逐实例箱数 `counts`、平均箱数 `mean_bins`、
逐实例装箱轨迹的 SHA256 `trace_hashes`。后者便于审计候选程序是否在完整轨迹上行为不同。
它不是安全隔离器；不可信生成代码应由上层受限执行器运行。

## 冻结的数据划分

| 划分 | 实例数 | 每例物品数 | 箱容量 | 每例随机种子 | 用途 |
|---|---:|---:|---:|---|---|
| development | 10 | 500 | 100 | 2026101800–2026101809 | 调试和探针 |
| search | 30 | 500 | 100 | 2026102800–2026102829 | 搜索期间的候选评分 |
| validation | 10 | 500 | 100 | 2026103800–2026103809 | 选定最后候选 |
| test | 30 | 500 | 100 | 2026104800–2026104829 | 冻结选择后的最终评估 |
| OR3 | 20 | 500 | 150 | 采用公开数据原顺序 | 额外独立基准 |

四个合成划分均使用独立 `random.Random(seed)` 生成 500 个 `randint(20, 80)`，
表示包含端点的离散均匀分布。每个实例记录种子和容量/物品顺序的内容哈希。
生成时检查所有合成划分的内容不重复，不能用改名字掩盖重复实例。
不需要对这些整数进行训练或归一化。

保存的数据文件是复现的权威输入；种子便于解释生成过程，SHA256 用于确认文件未改变。
`manifest.json` 记录文件 SHA256、数据参数、各划分数量、探针来源、OR3 来源和许可证说明。
运行同一生成命令会核对并复用相同文件；遇到任何不同的已有目标文件会报错，不自动覆盖。

## 行为探针

`probes.json` 中有 128 个互不重复的 `(item, bins)` 状态，全部来自 development 的合法装箱轨迹。
默认先收集 FF、BF；若仍不足才补充 Worst Fit。每个探针至少有两个可行箱子，
因此候选函数至少有选择空间。重复残量允许存在，它们代表不同的真实箱子。
保留箱子原顺序，因为顺序会影响 FF 和并列分数的选择。

使用种子 `2026105800` 从唯一状态池中抽取并固定顺序。前 32 个为第一阶段，
剩余 96 个为第二阶段；每个状态保存 development 实例 ID、策略、物品位置。
没有使用 search、validation、test 或 OR3 构造探针。
128 次选择相同只是有限的筛查证据，**不是两个程序全局等价的证明**。

## OR3 来源与许可

程序以 JSON 读取官方 FunSearch notebook，用 `ast.literal_eval` 提取其中的
`datasets['OR3']` 字典；不会执行 notebook 的任何代码，也不会载入其中的已发现启发式。
核验 notebook 旁的原始下载 `manifest.json`，并要求 notebook SHA256 为：

```
eec45846a8661722addbb856179327cf2ede0bc502e32be4ad58a6ac54e0aca6
```

保持 `u500_00` 到 `u500_19` 的实例顺序及每个实例的物品顺序。
上游仓库：[Google DeepMind FunSearch](https://github.com/google-deepmind/funsearch)。
数据原始说明：[OR-Library bin packing](https://people.brunel.ac.uk/~mastjjb/jeb/orlib/binpackinfo.html)。
仓库声明软件采用 Apache-2.0，其他材料采用 CC-BY-4.0；导出数据保留这一声明及出处，
但尚未独立核实 OR-Library 是否另有数据许可条款。对外发布数据时应保留归属与许可说明。
引用 Romera-Paredes et al. (2023), DOI `10.1038/s41586-023-06924-6` 及 OR-Library/J. E. Beasley。

## 生成和读取

在项目 `implementation` 根目录执行：

```powershell
python scripts/prepare_data.py --out-dir data_reproduced --notebook references/funsearch/bin_packing.ipynb
python -m unittest discover -s tests -p test_packing_data.py -v
```

正常输出包括 development=10、search=30、validation=10、test=30、OR3=20、probe_count=128，
以及 `heldout_evaluated: false`。第二次运行得到同样数据，不会重新随机采样。
省略 `--notebook` 可仅准备合成数据；若之后加入 OR3，应在新目录生成完整数据，避免修改已冻结清单。

`load_instances(path)` 会校验数据结构、集合哈希和每个实例哈希。
`heldout/test.json` 和 `heldout/or3.json` 默认拒绝读取；只有明确传入
`allow_heldout=True` 才可读。`load_probes(path)` 检查探针数量、哈希、来源和唯一性。
这是一道防止误用的流程约束，不是加密或安全访问边界。

准备数据、核验文件结构不等于评估算法。开发与调参阶段不应运行 test/OR3，
最终候选和配置冻结后再评估，看到结果后不得继续用它们调 Prompt 或筛查参数。
