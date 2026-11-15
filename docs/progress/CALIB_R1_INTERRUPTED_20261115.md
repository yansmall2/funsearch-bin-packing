# 校准第 1 轮中断记录（2026-11-15，运行时重启）

## 发生了什么

- 2026-11-15 16:05 启动的校准（四组各 32 次，seed 11）在 16:21 左右被一次运行时重启
  打断，四个实验进程全部死亡（`ps` 确认无 experiment.py 进程；四个 exec 会话均无确认的
  最终结果）。这不是实验本身失败，是基础设施重启。
- 中断时进度（以各组 progress.json / evaluated.jsonl 为准）：
  - A：25/32 请求，最优候选 265.3 箱（与 BF 持平，无改进）
  - B：21/32 请求，最优候选 **265.133 箱（candidate_000012，优于 BF 265.3）**
  - C：25/32 请求，最优候选 265.3 箱（与 BF 持平）
  - D：18/32 请求，最优候选 265.833 箱（差于 BF）；request 19 挂在 pending，未收到回答
- B 组最优候选代码（search 集实测 265.133）：
  `def priority(item, bins): return [1000.0 if r - item == 0 else (-(r - item) - 100.0 if r - item > item / 3 and r - item <= item else -(r - item)) for r in bins]`
  机制：精确装满重奖；残留落在 (item/3, item] 区间重罚。
- 4 个生成器 coordinator 全部存活（运行时恢复了 subagent），只是在等已死的旧进程
  的新请求；已将其重定向到第 2 轮。

## 恢复方式

- 第 1 轮的 run 目录（run_muse_calib_20261115_{A,B,C,D}）与握手目录（handshake-calib/）
  原样保留为中断证据，不删不改。
- 第 2 轮全新重跑：新 out_dir（run_muse_calib_20261115_{A,B,C,D}_r2）、新握手目录
  （handshake-calib-r2/），同样 seed 11、四组各 32 次、mechanism_v3 +
  disable_on_mismatch_v1。第 2 轮是完整的 32 次运行，可独立作为校准结果；
  第 1 轮的 89 次已完成请求只作参考，不并入第 2 轮统计。
- 费用仍为 0 元（Muse 生成器，无付费调用）。
