# Reproduce the milestone

Run these commands from `code/`. The reference environment was Windows 11 and CPython 3.13.5. The project uses the Python standard library. The submitted comparison can be verified offline with the bundled search, validation and probe data.

## Reproduce the report

| Command | Expected result | Typical time |
| --- | --- | --- |
| `python -B results.py --budget 60` | Rebuild 720 prompts, check the random audits, repack the shortlist and verify each of the 12 validation winners. All tie BF at 267.7. | Under 2 min |
| `python -B results.py --budget 200` | Rebuild 2,400 prompts and 12 selections. On these same trajectories, search improves after step 60 in 9/12 runs and validation improves in 1/12. | Under 2 min |
| `python -B results.py --final60` | Print the previously recorded synthetic test and OR3 counts for every sealed 60-step winner. | Seconds |
| `python -B results.py --history` | Re-score two pilot candidates against BF, then print the earlier fixed-stream filter comparison (402 vs 460 full evaluation attempts). | Seconds |
| `python -B -m unittest discover -s tests -q` | Exercise the simulator, screening, prompt, protocol and multi-process ledger checks. One optional test skips if the upstream notebook is absent. | About 10 s |

Omit `--budget` to verify both studies. Add `--all-search` to recompute every measured candidate's sequence counts and action-trace digest. Without that switch, all shortlisted search results and all validation selections are recomputed. The four executable programs that coordinate the pipeline are `experiment.py`, `formal.py`, `audit_and_cost.py` and `results.py`.

## What the public evidence contains

The [60-step records](evidence/search-60.json) and [200-step records](evidence/search-200.json) are ordinary JSON. They contain the scoring functions that completed full search evaluation, per-sequence integer counts, anonymous parent indices, original prompt SHA-256 digests, audit deltas, the returned model identifier, timing and cost statistics. Code and repeated count vectors each have a shared lookup table. Positive indices are the generation steps. The two negative parent indices identify the FF and BF seeds. Field names and status values are defined in each file.

The verifier rebuilds a prompt using its recorded parent functions, their measured counts and the frozen template. Its digest must equal the digest of the actual messages saved at generation. It recomputes random audits against the representative available at the time of the decision, and checks the selected winner's code hash. Programs that failed or were skipped before full evaluation are represented by status and provenance. Original unfiltered model responses and full prompt logs are kept in the local research record.

Read the [60-step freeze](evidence/protocol-60.json), [200-step freeze](evidence/protocol-200.json) and [60-step final outcomes](evidence/final-60.json) for the model settings, data checksums, source checksums and selection seal. The 200-step extension stopped at validation. The earlier Muse study also evaluated the same benchmark data.

A/C use the basic prompt. B/D use the mechanism prompt. C/D screen after exact-source matching. New sources that match BF on all 128 probes are evaluated in full. A seeded 10% audit applies to other complete fingerprint matches. An off-probe trace or count disagreement disables that fingerprint for the rest of the run. Tied scores choose the earliest opened feasible bin. Each generation within a run depends on the previous search results.

There were 3,120 formal responses and an estimated CNY 8.067984 in known token usage. Twelve additional requests with unknown usage retained CNY 0.278352 in reservations. The ledger therefore accounted for CNY 8.346336. The [60-step recovery](evidence/credential-recovery.json) and [200-step extension](evidence/extension-200.json) record the cumulative budget. For efficiency, per-run times include probe setup, screening, audits and validation. Four workers overlap. The separate study wall times were 495.4 and 1,868.2 seconds.

## Optional historical replay

The [paired replay results](evidence/replay.json) describe fixed proposals from the publicly archived Muse experiment. Its generator-reported model was muse-spark-1.3 and token use was unavailable. To rerun all 1,200 historical proposals, obtain the pinned historical checkout first, then run the local evaluator. On the reference machine the four-worker comparison took about seven minutes.

```sh
git clone --single-branch --branch fatbaby/smoke-test-20261114 https://github.com/yansmall2/funsearch-bin-packing.git ../muse-archive
git -C ../muse-archive checkout 2aba932339600c42ca76c9251deb2c3e6ffe2db6
python -B replay_archive.py --archive-checkout ../muse-archive --workers 4 --out replay-local.json
```

## Data and optional final-benchmark replication

`data/` includes the development, search, validation and probe files and the manifest. All four are generated deterministically. The public submission provides a checksum-pinned download script for the upstream FunSearch notebook containing OR3. The script parses the notebook as data. It does not execute notebook code. Source and attribution details are in [Third-party notices](THIRD_PARTY_NOTICES.md).

To regenerate the synthetic data alone in a new directory:

```sh
python -B scripts/prepare_data.py --out-dir data_reproduced
```

To independently reproduce the already published 60-step final numbers, fetch the upstream input and use a new directory:

```sh
python -B scripts/fetch_or3_source.py
python -B scripts/prepare_data.py --out-dir data_with_or3 --notebook references/funsearch/bin_packing.ipynb
python -B results.py --verify-final60-data data_with_or3
```

The verifier checks the benchmark hashes and runs only the 60-step sealed sources. Repacking the optional benchmarks typically takes under two minutes after download.

## Optional new paid replicate

Use a fresh checkout for each budget and CPython 3.13.5. The following 200-step example requires an authorized DeepSeek key in the local environment variable `FUNPACK_API_KEY`; an interactive terminal prompts if it is absent. Review the monetary and request limits before running. The higher request caps in the public configurations include historical ledger attempts. The schedule below requests 2,400 new candidates.

```sh
python -B formal.py run --protocol evidence/protocol-200.json --api-config configs/deepseek-200.json --data data --out_dir run_new_200 --parallel-arms --allow-paid
python -B formal.py seal --protocol evidence/protocol-200.json --study run_new_200 --data data --output run_new_200/candidates-sealed.json
python -B audit_and_cost.py --study run_new_200 --data data --out-dir run_new_200/audit
```

For the 720-request study use the 60-step protocol and API configuration and a fresh output directory. On the original runs, 720 calls took 495.4 seconds and 2,400 calls took 1,868.2 seconds. Wall time depends on provider latency and contention. The measured phases include probe setup, screening, audits and validation. Per-run durations overlap under parallel execution.
