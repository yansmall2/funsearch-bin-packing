# A 组 v5b 校准 r1 中断记录（2026-11-16 凌晨）

## 经过

- A 组 seed 11 × 32 次于 19:05 UTC 启动（`run_muse_ctxv5b_A_seed11/`，
  握手 `handshake-ctxv5b/A_seed11/`）。
- 19:27:57 UTC 发出第 28 条请求；约 19:28 UTC 运行时重启
  （uptime 事后核实为 5 分钟），实验进程被杀。
- 中断时状态：`progress.json` 停在 request 27（best 265.133，
  0 provider 错误）；`pending_request.json` 存在（第 28 条已发出，
  未被消费）；requests/ 与 responses/ 各 28 个文件；无
  `final_info.json`。
- 生成器协调员的第 27 条写入命令被重启中断，事后逐项核对：
  `responses/request_000027.json` 与手写原文一致，
  `generations.jsonl` 恰好 27 行（无重复），无残留 `.tmp`，
  重建校验脚本后重新校验第 27、28 条均通过。无损坏、无重复写入。
- 第 28 条回答已写入但从未被实验进程消费。
- C 组已在重启前完成（`final_info.json` 完整，
  termination `request_budget_completed`，best 265.133），不受影响；
  B 组 v5 校准亦早已完成，不受影响。

## 处理

`run_search` 无法原地续跑，按既往做法（校准 R1、热启动 r3/r4）
全新重跑：A 组 r2，新输出目录 `run_muse_ctxv5b_A_seed11_r2/`，
新握手目录 `handshake-ctxv5b-r2/A_seed11/`，同一配置（seed 11，
32 次，mechanism_v5b，disable_on_mismatch_v1），新生成器协调员。

r1 的部分证据保留，不并入 r2 的判定：27 次已评估，最好
265.133（与 C 组最终最好相同），0 错误。预注册标准以 r2
完整 32 次为准。
