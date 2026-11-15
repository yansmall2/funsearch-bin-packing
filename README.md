# Prompt Design and Behavioral Screening for FunSearch

CS5491 课程项目：用 A/B/C/D 四组对照研究提示词设计与行为筛查对在线装箱程序搜索质量及成本的影响。这是小规模 FunSearch-inspired 实现，不是原论文完整系统或性能复现。

## 当前进度：2026-11-15

DeepSeek `deepseek-flash` 开启低强度思考，固定搜索种子 11。四组各完成 50 次真实调用，接口错误为 0。这是单种子预实验，最终测试集和 OR3 尚未用于评估。[配置、分组指标、审计与可重放样例](docs/progress/low200_20261115.json)

| 组别 | 成功完整评估候选 | 指纹跳过 | 搜索最优平均箱数 | 入选程序验证平均箱数 |
|---|---:|---:|---:|---:|
| A：基础提示 | 47 | 0 | 265.300 | 267.7 |
| B：增强提示 | 47 | 0 | 265.200 | 267.6 |
| C：基础提示＋筛查 | 24 | 24 | 265.267 | 267.7 |
| D：增强提示＋筛查 | 35 | 14 | 265.300 | 267.7 |

平均箱数越少越好。Best Fit（BF）在 30 条搜索序列上平均 265.3 箱，在 10 条验证序列上平均 267.7 箱。B 组第 41 个候选在搜索集的 3 条序列、验证集的 1 条序列上各省 1 箱，其余持平。A、D 最终都选中了初始 BF。验证集用于挑选候选，不能代替独立测试。

- **提示效果：** 在未启用筛查的 A、B 两组，分别有 0 和 12 次生成的候选在搜索集上严格胜过 BF。同一次搜索里的候选会互为父代，不能把它们当成独立重复。
- **筛查效果：** 原定随机审计中，C 有 1/8、D 有 2/3 的候选与指纹代表在至少一条序列上用了不同箱数。另将全部 38 个实际跳过的候选放回搜索集复算：D 的第 3 个候选在 128 个探针上都和 BF 一样，却在三条序列上各省 1 箱，搜索均值为 265.200。它当时没有参加验证选择；表格保留 D 在原实验中的成绩。
- **成本：** 本批约 3 小时 44 分，按报告的 token 用量估算 ¥16.46（非账单）。模型生成占总时间约 99.1%。筛查减少了完整评估次数，两对分组的总耗时变化方向不同。分组耗时已计入探针、审计和验证选择。

## 离线核对本次成绩

在仓库根目录启动 Python，执行以下代码即可用已提交的合成数据重新装箱，不调用 API。它检查 BF、两个入选程序（B/41、C/41）和两个当时被跳过的程序（C/5、D/3）。被跳过的程序只核对搜索集。

```python
import json
from pathlib import Path
from funpack.data import load_instances
from funpack.packing import evaluate
from funpack.programs import Program

report = json.loads(Path("docs/progress/low200_20261115.json").read_text(encoding="utf-8"))
sets = {s: load_instances(f"data/{s}.json") for s in ("search", "validation")}
for name, case in report["examples"].items():
    for split, expected in case["mean_bins"].items():
        actual = evaluate(Program(case["priority"]), sets[split])["mean_bins"]
        assert abs(actual - expected) < 1e-6, (name, split, actual, expected)
        print(f"{name:>4} {split:10} {actual:.3f}")
```

`scripts/run_thinking_pilot_200.py --low` 是当日使用的运行入口，需要原实验的本地校准记录及账本。以上重放独立于这些私有文件；它核对候选得分，不重现模型生成过程。

## 离线运行

使用 Python 3.12 或 3.13（本地验证为 3.13.5），在仓库根目录运行：

```powershell
python -m unittest discover -s tests -v
python experiment.py --provider offline --calls 4 --seeds 11 --enhanced-prompt-version mechanism_v3 --collision-policy disable_on_mismatch_v1 --out_dir run_demo_v3
python formal.py --help
```

核心代码只依赖标准库。离线候选是人工固定样例，只验证流程，不检验 Prompt 效果。输出目录必须是新的。绘图时安装 `requirements.txt` 中的依赖。

## 代码导航

| 位置 | 用途 |
|---|---|
| `funpack/` | 装箱模拟、受限程序解释器、提示、搜索、筛查、API 与持久预算账本 |
| `experiment.py` | 四组搜索与验证选择入口 |
| `formal.py` | 协议冻结、候选封存及最终测试入口 |
| `audit_and_cost.py`、`replay.py`、`plot.py` | 审计与成本分析、重放、绘图 |
| `scripts/` | 数据准备、连接检查、分阶段实验与离线验证 |
| `configs/` | 离线配置、禁用的 API 模板及未启用的正式计划 |
| `tests/` | 离线单元测试与集成测试 |

## 历史记录

- [11 月 14 日代码与进度快照](docs/progress/STATUS_20261114.md)
- [11 月 9 日网络失败与未知费用说明](docs/progress/thinking_failure_20261109.json)
- [11 月 14 日测试记录](docs/progress/tests_20261114.json)

## 实验与证据边界

A/B 不做行为筛查，C/D 使用 32＋96 个开发状态及 10% 随机审计；B/D 使用增强提示。四组共享 FF/BF 初始程序，搜索、验证和最终测试分离。有限状态一致不等于全局等价；未完整评估的候选不虚构成绩。受限 Python 子集不支持 NumPy、导入、文件、网络或任意循环，这限制了搜索空间。

`python scripts/check_deepseek_connection.py` 可不带密钥检查网络路径。付费实验入口另需本地凭据及阶段记录；不要删除 claim 或重置账本来重跑历史实验。同步不会自动启动模型调用。

私有配置、密钥、账本、原始运行日志、第三方 notebook 和个人草稿不包含在本次同步中。数据及第三方来源见 [数据说明](docs/DATA.md)、[来源恢复说明](references/funsearch/README.md) 和 [协作与归属](THIRD_PARTY_NOTICES.md)。
