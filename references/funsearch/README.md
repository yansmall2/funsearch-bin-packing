# 上游材料的获取与恢复

本仓库保留来源清单，不分发官方 notebook 或 OR3 数据副本。开发、离线搜索和大部分测试无需它们。
上游地址为 https://github.com/google-deepmind/funsearch 。归属与许可见根目录 `THIRD_PARTY_NOTICES.md`。

需要完整复现 OR3 数据准备时，在仓库根目录运行：

```powershell
python scripts/fetch_or3_source.py
python scripts/prepare_data.py --out-dir data --notebook references/funsearch/bin_packing.ipynb
```

第一步仅下载已知官方来源并核对固定 SHA256，不执行 notebook；如上游内容改变则拒绝保存。
第二步按已有清单复用相同数据并补齐缺失 OR3，遇到已改动文件则拒绝覆盖。
两步均不调用大模型，也不评估算法。真实 test/OR3 只在正式候选封存后使用。

固定 notebook SHA256：

```
eec45846a8661722addbb856179327cf2ede0bc502e32be4ad58a6ac54e0aca6
```

来源清单记录此前核验的 URL 和哈希，不表示其中每份上游材料都随仓库分发。
如官方 main 文件发生变化，需要另行定位原始对应版本，不能取消校验或改哈希凑过。
