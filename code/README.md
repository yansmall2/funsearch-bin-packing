# Reproduce the milestone

Run these commands from `code/`. The reference environment was Windows 11 and CPython 3.13.5. The project uses the Python standard library. The submitted comparison can be verified offline with the bundled search, validation and probe data.

## Reproduce the report

| Command | Expected result | Typical time |
| --- | --- | --- |
| `python -B results.py --budget 60` | Verify the independently started 60-step study. Rebuild 720 prompts and all 12 validation winners. All tie BF at 267.7. | Under 2 min |
| `python -B results.py --budget 200` | Rebuild 2,400 prompts and all 12 validation winners. A second table compares step 60 and step 200 within each long run. | Under 2 min |
| `python -B results.py --final60` | Print the previously recorded synthetic test and OR3 counts for every sealed 60-step winner. | Seconds |
| `python -B results.py --history` | Re-score two pilot candidates against BF, then print the earlier fixed-stream filter comparison (402 vs 460 full evaluation attempts). | Seconds |
| `python -B -m unittest discover -s tests -q` | Exercise the simulator, screening, prompt, protocol and multi-process ledger checks. One optional test skips if the upstream notebook is absent. | About 10 s |

Each step is a request to propose one candidate rule. Both budgets cover four groups and three seeds. The 200-step prefix comparison reuses the first 60 requests from each long run and repeats validation selection using the candidates available then. In that comparison, search improves in 9 of 12 runs and validation improves in 1 of 12.

Omit `--budget` to verify both studies. Add `--all-search` to recompute every measured candidate's sequence counts and action-trace digest. The default rechecks shortlisted search results, audited matches and all validation selections.

| Entry point | Use |
| --- | --- |
| `results.py` | Verify published results offline. |
| `scripts/run_study.py` | Check settings or start a new run of either DeepSeek preset. |
| `formal.py` | Freeze experiments, seal candidates and run final evaluation. It delegates search to `experiment.py`. |
| `experiment.py` | Lower-level search runner. Its default provider uses offline fixtures. |
| `milestone.py` | Historical launcher for the initial 50-step plan (600 requests total). Published protocols pin its source hash. Its `check` command verifies the archived plan. |

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

From `code/` with CPython 3.13.5, check both presets offline first.

```sh
python -B scripts/run_study.py check
```

All files under `configs/deepseek*.json` read the DeepSeek key from `DEEPSEEK_API_KEY`. Both presets send requests to the official DeepSeek endpoint with `deepseek-flash` and thinking disabled. In an interactive terminal, the runner prompts for the DeepSeek key if the variable is unset.

The signed records in `evidence/` retain the variable names used when the original runs were frozen. The launcher converts the variable name for the frozen runner and saves the effective, credential-free configuration alongside the new study. The underlying provider and API settings come from the selected preset.

The 60-step preset schedules 720 new requests and the 200-step preset schedules 2,400. Both use a CNY 100 ceiling. The stored caps of 724 and 3,132 include earlier attempts from the original ledger. A new run uses a separate ledger inside its output directory.

```sh
python -B scripts/run_study.py run --budget 200 --out-dir run_new_200 --allow-paid
python -B formal.py seal --protocol evidence/protocol-200.json --study run_new_200/study --data data --output run_new_200/study/candidates-sealed.json
python -B audit_and_cost.py --study run_new_200/study --data data --out-dir run_new_200/audit
```

Use `--budget 60`, `run_new_60` and `evidence/protocol-60.json` for the shorter study. On the original runs, 720 requests took 495.4 seconds and 2,400 requests took 1,868.2 seconds. The measured phases include probe setup, screening, audits and validation. Per-run durations overlap under parallel execution.
