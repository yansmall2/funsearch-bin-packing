# Prompt Design and Behavioral Screening for FunSearch in Online Bin Packing

CS5491 Artificial Intelligence · Group G004 · Project milestone, November 2026

Can a language model find a better rule for selecting a bin, and how many candidate rules do we need to evaluate? We compare a basic prompt with one that asks when a bin choice would change. We also test whether a short set of probes can identify candidates worth skipping.

| Start here | Contents |
| --- | --- |
| [Milestone report draft (PDF, 3 pages)](report/milestone.pdf) | Method, per-seed results and the screening audits |
| [Reproduction guide](code/README.md) | Environment, commands, expected output and data provenance |
| [Experiment evidence](code/evidence) | Frozen protocols, measured candidate programs, prompt checksums and archived outcomes |

## What the experiments found

Each study had four groups and three random seeds per group. One step is one DeepSeek Flash request for a candidate rule.

| Budget per condition and seed | Completed requests | Validation finding | Elapsed wall time |
| --- | ---: | --- | ---: |
| 60 steps | 720 | All 12 selected programs tied Best Fit at 267.7 mean bins. | 8.3 min |
| 200 steps | 2,400 | Two D runs reached 267.6. Each saved one bin on the same validation sequence. | 31.1 min |

Within the longer runs, we compared the candidates available at step 60 with those available at step 200. Best search scores improved in 9 of 12 runs and validation improved in 1 of 12. The 60-step study also has results on the synthetic test set and OR3.

## Reproduce the main comparison

From a fresh clone with Python 3.13.5, run the following. These commands use the bundled evidence and the Python standard library.

```sh
cd code
python -B results.py --budget 60
python -B results.py --budget 200
python -B results.py --final60
```

These commands verify the saved results offline. Use [the live-run instructions](code/README.md#optional-new-paid-replicate) to check API settings or start a new run.

The experimental history remains accessible at the [previous main revision](https://github.com/yansmall2/funsearch-bin-packing/tree/9a3438be998321112ed981f4a4e125007bde0f98) and the [archived Muse experiment](https://github.com/yansmall2/funsearch-bin-packing/tree/2aba932339600c42ca76c9251deb2c3e6ffe2db6). Earlier model configurations and benchmarks are identified separately in the report.
