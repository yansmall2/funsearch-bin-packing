# 正式协议修订：Muse 生成器（2026-11-15）

## 为什么改

正式比较原本只支持 DeepSeek API（`formal.py run` 硬编码 `--provider api`，
`_inspect_study` 要求 `paid_api_request == True`）。但：

- 2026-11-09：200 次 DeepSeek thinking 调用全部 SSL 失败，0 候选。
- 2026-11-15：云端后台到 DeepSeek 是策略封禁（sentinel 403），不是偶发故障。
- 校准（128 次，见 CALIBRATION_MUSE_20261115.md）已证明 Muse 握手生成器
  可行：128/128 合法代码，0 提供器错误，0 元。

不改这套机制，正式比较就跑不起来。改动全部在冻结之前完成，冻结后
（`protocol-formal-muse-100.json`）源码哈希即包含改动后的代码，冻结后
再改源码会使 `load_protocol` 拒绝加载——冻结纪律没有放宽，只是换了
一个同样受检的提供器。

## 改了什么（逐文件）

- `funpack/protocol.py`
  - `CORE_FILES` 增加 `funpack/handshake_provider.py`：握手提供器的代码
    也被冻结哈希 pin 住。
  - 新增 `normalize_handshake_config`：只认 `{provider, model,
    timeout_seconds, max_requests}`，禁任何密钥字段；无预算、无账本、
    无付费。
  - `draft_plan()` 增加 `"provider": "api"`（默认不变）；`_validate_plan`
    要求 provider 是 `api` 或 `handshake`。
  - `freeze_protocol`：plan provider 是 handshake 时读握手配置，要求
    `max_requests >= 总计划请求数`，协议里存 `provider_config`，
    `api_config` 置 None；api 时走老路不变。
  - `validate_runtime`：handshake 时要求 provider=handshake、stage=all、
    其余字段与计划完全一致；arms/seeds 允许是计划的非空子集（为了一次
    只跑一个 (arm, seed) 的分片进程，重启只丢一个分片）；provider 配置
    必须与冻结的一致。api 时保持原来的精确匹配。
  - `_inspect_study`（seal/evaluate 共用）：handshake 时要求
    `provider_class == "HandshakeProvider"`、`paid_api_requests == 0`、
    每条响应 `paid_api_request is False` 且 `requested_model` 等于冻结的
    模型；其余检查（12 组运行齐全、事件顺序、消息哈希、指标一致、
    验证选择规则）与 api 时完全相同。
- `experiment.py`
  - 新增 `--handshake-config`（正式握手跑必须给）；`--protocol` 是
    handshake 协议时用它做 `validate_runtime` 的配置比对，并把
    `provider_config` 写进 study.json；提供器超时取配置里的值。
- `formal.py`
  - `freeze` 增加 `--handshake-config`；`--api-config` 改为可选。
    `run` 仍只支持 api（正式 Muse 跑走 `experiment.py` 分片 + 合并，
    见下）；`seal` / `evaluate` 不变。
- 新增 `scripts/merge_formal_study.py`：把 12 个分片（每分片一个
  (arm, seed)）合并成一个 study 目录。每个分片先按冻结协议验过
  （phase、protocol_id、源码哈希、provider 配置、数据哈希、无提前
  停止、运行完整、无 pending 请求、指标与 final_info 一致）；运行
  目录原样复制，只重建 study.json（arguments 补全为完整计划，runs
  合并）。seal 时会对每个运行文件重新验一遍，合并藏不住东西。
- 新增 `configs/muse_handshake.json`：`{provider: handshake,
  model: muse-spark-1.3, timeout_seconds: 1800, max_requests: 1200}`。
- 新增 `configs/formal-plan.muse-100.json`：3 种子 [11, 22, 33]，
  每组每种子 100 次，共 1200 次；其余全部是 draft 默认值
  （mechanism_v3、disable_on_mismatch_v1、audit_rate 0.1、reset_every 50）。

## 没改的纪律

- 冻结在看到任何正式成绩之前；冻结后改源码、数据、模型、调用数，
  `load_protocol` / `_inspect_study` 都会拒绝。
- seal 之前必须 12 组运行齐全且全部 `request_budget_completed`。
- 最终测试只跑一次，`final_evaluation_claim.json` 防重复认领。
- 正式跑的 1200 次里，生成器只看每条请求自带的 messages；不看验证集、
  保留集、别的组。

## 正式流程（Muse 版）

```bash
python formal.py freeze --plan configs/formal-plan.muse-100.json \
  --handshake-config configs/muse_handshake.json --data data \
  --output protocol-formal-muse-100.json
# 12 个分片并行，每分片一个 (arm, seed)，各 100 次：
python experiment.py --stage all --provider handshake --protocol protocol-formal-muse-100.json \
  --handshake-dir formal-handshake --handshake-config configs/muse_handshake.json \
  --data data --arms A --seeds 11 --calls 100 --islands 4 --reset-every 50 \
  --audit-rate 0.1 --database-temperature 1.0 --enhanced-prompt-version mechanism_v3 \
  --collision-policy disable_on_mismatch_v1 --out_dir run_formal_part_A_seed11
# ... B/C/D × 22/33 同理 ...
python scripts/merge_formal_study.py --protocol protocol-formal-muse-100.json \
  --parts run_formal_part_* --data data --out run_formal
python formal.py seal --protocol protocol-formal-muse-100.json --study run_formal \
  --data data --output candidates-sealed.json
python formal.py evaluate --protocol protocol-formal-muse-100.json \
  --seal candidates-sealed.json --study run_formal --data data \
  --out_dir final_evaluation --allow-heldout
```
