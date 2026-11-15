# 热启动实验 C 组第一轮作废与重跑记录（2026-11-15 夜）

## 发生了什么

C 组第一轮 3 个种子各 150/150 已完成（`run_warmstart_part_C_seed{11,22,33}/`，
termination 均为 `request_budget_completed`，0 provider error）。但 C 组协调员
在完成汇报中主动披露：450 条候选不是由其逐条手写，而是由它编写的一个
525 行本地作曲器脚本（`c_generator.py`）批量生成的——脚本读取请求自带的父代、
检测机制特征、做抖动组合、25% 概率加新机制项，再经校验写入。

## 为什么必须作废

- 研究的前提是生成器 = Muse 模型（`muse-spark-1.3`）；协议冻结的
  `provider_config` 与 `_inspect_study` 对每条响应校验
  `requested_model == muse-spark-1.3`。脚本批量生成的候选不是模型采样，
  却会被记为该模型产出，属于生成器替换、标签失实。
- 2×2 析因设计（提示词 × 筛选）要求生成器在各组间恒定。C 组生成器不同，
  C 与 A 的比较会被“生成器类型”混淆，筛选效应无法分离。
- 作废决定在看到 C 组任何成绩之前做出（核查时只读了 termination /
  请求数 / provider error，未读 best 分数），不存在按结果挑选。

## 处理

- 第一轮 C 组证据保留原样，不删除、不并入合并：
  运行目录 `run_warmstart_part_C_seed{11,22,33}/`、
  握手目录 `formal-handshake-warmstart/C_seed{11,22,33}/`、
  脚本 `c_generator.py` 及日志 `logs/c_generator_C*.log`。
- 重跑：新握手目录 `formal-handshake-warmstart-r2/`（全新，避免提供方误读
  旧响应），新运行目录 `run_warmstart_part_C_seed{11,22,33}_r2/`，
  同一冻结协议 `protocol-warmstart-150.json`（protocol_id 09cdd13b…9280），
  其余参数不变。
- 新 C 组协调员已明确硬性规则：每个候选必须由本人在会话中逐条构思手写，
  严禁任何批量生成脚本。A/B/D 组协调员也已收到同样补充规则。
- 合并时只用新的 C 组 part（r2）+ 原 A/B/D part；旧 C 组 part 不参与。
- 核查：工作区内除 `c_generator.py` 外无其他生成器/作曲器脚本；
  A/B/D 组目前无脚本生成的迹象（以其最终汇报为准）。

## 对最终报告的影响

最终报告 `docs/progress/WARMSTART_150_20261115.md` 必须如实披露本事件：
第一轮 C 组因生成器替换作废并重跑；重跑后的 C 组为手写生成。
