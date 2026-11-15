# Muse 作为生成器：设计、验证与披露（2026-11-15）

## 为什么换生成器

- 2026-11-09：200 次 DeepSeek thinking 调用全部 SSL 失败，0 候选。
- 2026-11-15：云端后台到 DeepSeek 是策略封禁（sentinel 403），不是偶发故障；**不要在后台重试 DeepSeek，也不要换客户端/代理绕过**。
- 2026-11-15 用户决定：改在 Muse 云端跑，**用 Muse 自己的模型做生成器**，0 元，不再走 DeepSeek。

## 做法：文件握手提供器（不改核心实验逻辑）

- 新增 `funpack/handshake_provider.py`：`HandshakeProvider.generate(messages)` 把
  `run_search` 建好的原样 messages（mechanism_v3 / basic 提示、当前岛屿 parents）
  写成请求文件，等 Muse 侧生成器把候选文本写进响应文件，再返回标准结果字典。
- `experiment.py` 只加了一个 `--provider handshake` 选项（+ `--handshake-dir`、
  `--handshake-timeout-seconds`，默认 1800 秒）。`run_search`、筛选、记账、审计、
  岛屿重置逻辑一个字没动；`formal.py` / `configs/research-stage.json` 没动。
- 生成器（一个常驻协调 agent）按请求文件顺序逐个生成：只读请求里的 messages，
  不看别的 arm 状态、不看验证集/保留集；响应文件原子写入（tmp + 改名）。
- 超时或响应文件无效时返回 error 字典，run 按“首个提供器错误即停”处理；
  绝不编造候选。

## 诚实标注（写进每一次运行的记录里）

- `provider=muse_handshake`，`model=muse-spark-1.3`，`paid_api_request=False`，
  `estimated_cost=0.0`，`cost_known=True`，货币 CNY。
- 同一次 Muse 运行内的 A/B/C/D 四组比较有效（四组用同一生成器，只差提示/筛选）；
  **绝对数值不能和 2026-11-06 的 DeepSeek 预实验直接比**（生成器换了）。
- 期中/期末报告必须写明这次生成器替换的原因（DeepSeek 传输失败 + 后台封禁）
  和上述比较边界。

## 冒烟验证（2026-11-15，已通过）

- 命令：`python3 experiment.py --stage search --provider handshake
  --handshake-dir handshake-smoke --arms A --seeds 11 --calls 1
  --enhanced-prompt-version mechanism_v3 --collision-policy disable_on_mismatch_v1
  --out_dir run_muse_smoke_20261115`
- 结果：`evaluated=1, text hits=0, behavior skips=0; API errors=0;
  best search=265.300`；候选（sliver-penalty 规则：残留 1–7 罚 20 分）
  实测 271.97 箱，差于 BF 265.3——假设被证伪，流程正确记下了它。
- 单元测试：`tests/test_handshake_provider.py` 4 通过（请求写入、响应返回、
  无效响应进 error、超时进 error、非法 run_name 拒绝）。

## 下一步（待用户确认后执行）

1. **校准 128 次**（四组各 32 次，seed 11，mechanism_v3 + disable_on_mismatch_v1）：
   四个 arm 各一个后台进程，各配一个握手目录；一个协调 agent 按序生成候选。
   目标：合法代码率、重复行为、随机审计样本数、费用（0 元）、等待时间。
   预计 128 次生成，协调侧按序处理，约 2–4 小时后台跑完。
2. 校准后按 `docs/FORMAL_PROTOCOL.md` 冻结正式实验（至少 3 个种子），再跑正式、
   验证选择并封存、一次最终测试、写报告。期中（11-19）先交校准 + 方法 + 诊断；
   正式多种子比较赶在 12-03 最终提交前完成。
3. 每步备份 + 推分支 `fatbaby/smoke-test-20261114`（只推本分支新增/改动文件，
   不动 main、正式协议、原账本）。
