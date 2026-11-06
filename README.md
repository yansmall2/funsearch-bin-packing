# Prompt Design and Behavioral Screening for FunSearch

CS5491 课程项目：通过四组对照，研究提示词设计与行为筛查对在线装箱（Online Bin Packing）程序搜索质量和成本的影响。
这是小规模 **FunSearch-inspired** 实现，不是原论文完整系统或其性能复现。

**2026-11-06：**完成 16 次真实 DeepSeek 接口预实验；正式实验协议与最终测试入口已实现，正式调用规模仍待校准。
尚未对真实 test/OR3 评估任何候选，也没有证据证明增强 Prompt 提高装箱质量。
详见 [项目状态](PROJECT_STATUS.md)、[预实验摘要](docs/PILOT_20261106.md)。

**2026-11-07 更新：**已准备 [128 次补充预实验入口](docs/CALIBRATION_20261107.md)。
默认只读预览；使用明确的授权参数后，才会保留原账本并扩展请求上限，累计费用上限仍为 10 元。

## 快速运行：离线、不收费

在仓库根目录使用 Python 3.12 或 3.13（本地验证版本 3.13.5）：

```powershell
python -m unittest discover -s tests -v
python experiment.py --provider offline --calls 4 --seeds 11 --out_dir run_demo
python formal.py --help
```

核心代码只依赖标准库。离线候选是人工固定样例，只检查流程，不能检验 Prompt 效果。
输出目录必须是新的；再次运行请换名字。`run_demo/study.json` 记录数据、源码、配置和每组结果。
绘图时再安装 `requirements.txt`，运行 `python plot.py --run run_demo`。

最小例子：容量 10，物品依次 `[6, 7, 3, 4]`，First Fit 使用 3 箱，Best Fit 使用 2 箱。
LLM 编写 `priority(item, bins)`，输入当前物品和可用箱子的剩余容量，输出各箱评分。
模拟器选最高分，同分选最早打开的箱子；所有已有箱子都放不下时才开新箱，不提供未来物品。

## 四组比较

| 组 | Prompt | 精确代码缓存 | 行为筛查 |
|---|---|---|---|
| A | 基础 | 有 | 无 |
| B | 最多 80 词策略说明，再给代码 | 有 | 无 |
| C | 基础 | 有 | 有 |
| D | 最多 80 词策略说明，再给代码 | 有 | 有 |

C/D 使用开发集产生的 32＋96 个状态指纹；看似重复的程序有 10% 概率接受完整随机审计。
有限状态上一致不等于程序全局等价。未完整测量的候选不获得虚构成绩，不进入成绩数据库。
各组共享 FF/BF 初始程序、数据、评分语言与父代选择机制。

## 模块与数据

| 文件 | 作用 |
|---|---|
| `funpack/packing.py` | 装箱规则、FF/BF、逐实例箱数和轨迹 |
| `funpack/programs.py` | 受限 AST 解释器，不用 exec/eval 执行生成源码 |
| `funpack/prompts.py` | 两种提示模板 |
| `funpack/providers.py`、`ledger.py` | API 响应、用量、持久化预算与失败记录 |
| `funpack/database.py`、`screening.py`、`search.py` | 多岛搜索、筛查、审计、日志 |
| `experiment.py` | 搜索、验证集选择、原始耗时 |
| `formal.py`、`funpack/protocol.py` | 规则冻结、候选封存、最终测试 |
| `scripts/run_calibration_pilot.py` | 128 次校准的只读预览、明确启用、预算延续与结果摘要 |
| `audit_and_cost.py` | 逐实例审计差异和搜索成本分析 |

合成数据划分为开发/搜索/验证/测试 10/30/10/30 个实例，每例 500 个物品；探针只来自开发集。
文件 SHA256、生成规则和来源见 [数据说明](docs/DATA.md)。
仓库不分发第三方 notebook/OR3 副本，离线搜索无需它们。正式测试前按
[来源与恢复说明](references/funsearch/README.md) 恢复。数据准备和结构核验不等于算法评估。

## 真实实验与结果边界

先阅读 [API 设置](docs/API_SETUP.md)、[正式实验操作顺序](docs/FORMAL_PROTOCOL.md)。
`configs/formal-plan.draft.json` 的 `calls_per_run` 故意留空，当前不能直接冻结。
示例 API 配置默认关闭，真实密钥仅通过本地环境变量或终端隐藏输入传入。
本次代码上传没有修改原预实验额度，也没有新增付费请求。

校准后固定模型、每次搜索调用数与审计参数，运行四组、每组至少三个种子；
用验证集选候选并封存，再统一评估 test/OR3。分开报告搜索管线耗时、全程耗时和 token 费用估算。

受限 Python 子集不支持 NumPy、导入、文件、网络或任意循环，这限制了搜索空间，需写入报告。
代码和文档有 AI 协作，来源见 [协作与归属说明](THIRD_PARTY_NOTICES.md)。
尚未选择开源许可证；私有仓库备份不等于课程提交或最终结果发布。
