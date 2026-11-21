# Prompt Design and Behavioral Screening for FunSearch in Online Bin Packing

CS5491 Artificial Intelligence · Group G004 · Project milestone, November 2026

We compare two ways to guide a language model's search for online bin-packing programs. A mechanism prompt asks when a rule will choose a different bin. A two-stage screen compares bin choices before deciding which candidate programs to evaluate fully.

| Start here | Contents |
| --- | --- |
| [Milestone report (PDF, 4 pages)](report/milestone.pdf) | Research questions, method, per-seed results, audits, timing and limitations |
| [Reproduction guide](code/README.md) | Environment, commands, expected output and data provenance |
| [Experiment evidence](code/evidence) | Frozen protocols, measured candidate programs, prompt checksums and archived outcomes |

## What the experiments found

Each study had four conditions and three search seeds. All 3,120 formal responses came from DeepSeek Flash with thinking disabled.

| Budget per condition and seed | Completed requests | Validation finding | Elapsed wall time |
| --- | ---: | --- | ---: |
| 60 steps | 720 | All 12 selected programs tied Best Fit at 267.7 mean bins. | 8.3 min |
| 200 steps | 2,400 | Two D runs reached 267.6. Each saved one bin on the same validation sequence. | 31.1 min |

Comparing each 200-step trajectory with its own first 60 steps, 9 of 12 best search scores improved. Validation improved once and tied eleven times. The 200-step winners were sealed at validation. The report gives the previously completed final tests of the 60-step winners.

## Reproduce the main comparison

From a fresh clone with Python 3.13.5, run the following. These commands use the bundled evidence and the Python standard library.

```sh
cd code
python -B results.py --budget 60
python -B results.py --budget 200
python -B results.py --final60
```

The experimental history remains accessible at the [previous main revision](https://github.com/yansmall2/funsearch-bin-packing/tree/9a3438be998321112ed981f4a4e125007bde0f98) and the [archived Muse experiment](https://github.com/yansmall2/funsearch-bin-packing/tree/2aba932339600c42ca76c9251deb2c3e6ffe2db6). Earlier model configurations and benchmarks are identified separately in the report.
