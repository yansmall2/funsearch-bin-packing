# 从预实验到正式比较

冻结就是在看到正式成绩前写定比较规则并记录哈希。例如每次搜索固定 N 次调用，
不能发现某组不好后只给它追加次数；候选也必须在看到 test/OR3 前选定。
当前机制已实现，真实正式配置尚未冻结。先看 `configs/formal-plan.draft.json` 的待定调用数。

## 校准与冻结

建议额外四组×32 次＝128 次校准，保持两种 Prompt、32＋96 探针、10% 审计不变。
检查有效候选率、新行为比例、随机审计样本数、token 与等待时间。
32 次不触发第 50 次的岛屿重置，不能据此判断长期搜索饱和。
若大多为 BF 的同义改写，先解释实现限制，不能只保留有利结果。
该轮尚未执行；原 16 次请求额度已用完，新增调用需确认并保留累计费用记录。

## 准备完成后再执行的命令

以下为未来命令模板，正式私有配置尚未创建，不要随意填数启动。
所有命令在仓库根目录运行；实际本地目录为 `course-project/implementation`。

1. 校准后确定调用数、至少三个种子、模型与采样配置、已批准预算；核对最新接口和价格。
   冻结无需密钥，不发请求。GitHub 克隆版先按 `references/funsearch/README.md` 恢复 OR3。

```powershell
python formal.py freeze --plan configs/formal-plan.draft.json --api-config configs/formal.local.json --data data --output protocol-frozen.json
```

2. 冻结后不能改源码、数据、模型或调用数。入口复核哈希和剩余请求槽；只有显式 `--allow-paid` 才能收费。
   未设置环境变量时终端会隐藏输入密钥，不写入日志。

```powershell
python formal.py run --protocol protocol-frozen.json --api-config configs/formal.local.json --data data --out_dir run_formal --allow-paid
```

3. 四组全部种子完成后，每次搜索的前五候选按验证均值选择，同分按搜索均值、代码长度、哈希决定。
   封存会拒绝 pilot、人工离线结果、不完整运行和改动的候选。

```powershell
python formal.py seal --protocol protocol-frozen.json --study run_formal --data data --output candidates-sealed.json
```

4. 统一评估一次，保留每个种子的逐实例箱数、FF/BF 与相对 BF 差值。负差值表示更少箱数。
   实例运行失败保留错误，该候选该划分均值不计算，不允许换候选补救。

```powershell
python formal.py evaluate --protocol protocol-frozen.json --seal candidates-sealed.json --study run_formal --data data --out_dir final_evaluation --allow-heldout
```

加载保留数据前创建 `run_formal/final_evaluation_claim.json`。中断时保留标记与部分结果，
检查日志并记录恢复方案，不能删标记反复挑选有利成绩。这是防误用机制，不是防恶意改文件的安全系统。

## 时间与费用

正式运行重建开发探针并核对哈希，构造成本分摊给 C/D；公共准备成本分摊到全部计划运行。
`pipeline_seconds` 包含种子评估、生成、筛查、随机审计、日志和验证选择。
`inclusive_search_seconds` 是管线＋分摊准备时间，**不等于完整端到端时间**。
`study_elapsed_seconds` 另包含基线报告、源码清单和顶层记账。分项不能重复相加。

```powershell
python audit_and_cost.py --study run_formal --data data --out-dir analysis_formal
```

旧日志的探针重测只是估计；token 单价估算与平台账单分开报告。
同时展示全程耗时与评估计算耗时，避免 API 延迟掩盖筛查的影响。
