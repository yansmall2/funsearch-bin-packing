# Prompt Design and Behavioral Screening for FunSearch

CS5491 课程项目：用 A/B/C/D 四组对照研究提示词设计与行为筛查对在线装箱程序搜索质量及成本的影响。这是小规模 FunSearch-inspired 实现，不是原论文完整系统或性能复现。

## 当前进度

2026-11-14 整理同步：当前代码、运行所需配置模板、测试和三份必要进度记录。

- 已实现机制导向 Prompt `mechanism_v3` 和审计碰撞后停用完整指纹的 `disable_on_mismatch_v1`。
- 已加入首个接口错误停止当前运行及后续组、连接预检、进程内直连选项及有效生成状态汇总。
- 最近一轮四组各 50 次请求全部 SSL 连接失败，有效候选为 0。累计尝试 408 次，包含这 200 次失败。四组 265.3 来自初始 BF，不能证明新方法有效。
- 55.4464 元是未知费用的保守占位，非确认扣费；此前成功实验估算为 0.447984 元，非平台账单。
- 正式多种子实验仍禁用，真实 test/OR3 未用于算法评估。下一步为恢复连接、核实未知费用，再用独立实验记录开展新的生成实验。

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

## 必要记录

- [当前进度](docs/progress/STATUS_20261114.md)
- [失败实验摘要](docs/progress/thinking_failure_20261109.json)
- [测试结果摘要](docs/progress/tests_20261114.json)

本地 187 项测试中 186 项直接通过；1 项受 Windows 沙箱 IPC 权限限制，沙箱外单独复测通过。仓库不包含可选第三方 notebook，相关校验可按现有测试规则跳过。已有其他文档保留历史阶段信息，最新状态以上述记录为准。

## 实验与证据边界

A/B 不做行为筛查，C/D 使用 32＋96 个开发状态及 10% 随机审计；B/D 使用增强提示。四组共享 FF/BF 初始程序，搜索、验证和最终测试分离。有限状态一致不等于全局等价；未完整评估的候选不虚构成绩。受限 Python 子集不支持 NumPy、导入、文件、网络或任意循环，这限制了搜索空间。

`python scripts/check_deepseek_connection.py` 可不带密钥检查网络路径。付费实验入口另需本地凭据及阶段记录；不要删除 claim 或重置账本来重跑历史实验。同步不会自动启动模型调用。

私有配置、密钥、账本、原始运行日志、第三方 notebook 和个人草稿不包含在本次同步中。数据及第三方来源见 [数据说明](docs/DATA.md)、[来源恢复说明](references/funsearch/README.md) 和 [协作与归属](THIRD_PARTY_NOTICES.md)。
