"""Plots from actual saved results. Requires matplotlib only for rendering."""
import argparse
import csv
import json
import statistics
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", default="run_1")
    args = parser.parse_args()
    root = Path(args.run)
    study = json.loads((root / "study.json").read_text(encoding="utf-8"))
    output = root / "figures"
    output.mkdir(exist_ok=True)
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    plt.rcParams.update({"font.size": 10, "axes.spines.top": False, "axes.spines.right": False,
                         "savefig.dpi": 300})
    colors = ["#0072B2", "#E69F00", "#009E73", "#CC79A7"]
    if (root / "baselines.json").exists():
        result = json.loads((root / "baselines.json").read_text(encoding="utf-8"))["results"]["search"]
        fig, ax = plt.subplots(figsize=(7, 3.8), layout="constrained")
        x = list(range(1, len(result["FF"]["counts"]) + 1))
        for name, color in zip(["FF", "BF"], colors):
            ax.plot(x, result[name]["counts"], marker="o", ms=3, lw=1, color=color, label=name)
        ax.set(xlabel="Search instance (frozen generation order)", ylabel="Bins used",
               title="Measured FF/BF baselines on the search set (not final test)")
        ax.legend()
        ax.grid(axis="y", alpha=0.2)
        fig.savefig(output / "Figure_1.png")
        fig.savefig(output / "Figure_1.pdf")
        plt.close(fig)
    rows = []
    for arm in "ABCD":
        runs = [run for run in study["runs"] if run["arm"] == arm]
        if not runs:
            continue
        row = {"arm": arm, "n_runs": len(runs), "phase": study["phase"]}
        for field in ("candidate_full_evaluations", "text_cache_hits", "behavior_skips", "audits",
                      "search_total_seconds", "evaluation_seconds", "screening_seconds"):
            values = [run["metrics"][field] for run in runs]
            row[field + "_mean"] = statistics.mean(values)
            row[field + "_sample_sd"] = statistics.stdev(values) if len(values) > 1 else ""
        rows.append(row)
    if rows:
        with (root / "summary.csv").open("w", newline="", encoding="utf-8-sig") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
        fig, ax = plt.subplots(figsize=(7, 4.5))
        fig.subplots_adjust(left=.13, right=.98, bottom=.23, top=.86)
        bars = ax.bar([row["arm"] for row in rows], [row["candidate_full_evaluations_mean"] for row in rows],
                      color=colors[:len(rows)])
        for bar, row in zip(bars, rows):
            ax.text(bar.get_x() + bar.get_width()/2, bar.get_height() + .15,
                    f"{row['candidate_full_evaluations_mean']:.1f}", ha="center")
        for index, row in enumerate(rows):
            values = [run["metrics"]["candidate_full_evaluations"] for run in study["runs"] if run["arm"] == row["arm"]]
            ax.scatter([index + (k-(len(values)-1)/2)*.07 for k in range(len(values))], values,
                       color="black", s=20, zorder=3)
        ax.set(xlabel="A basic | B enhanced | C basic+screen | D enhanced+screen",
               ylabel="Full evaluation attempts (audits included)",
               title="OFFLINE FIXTURES: pipeline checks only" if study["phase"] == "offline_integration" else "Pilot evaluation counts")
        ax.set_ylim(0, max(row["candidate_full_evaluations_mean"] for row in rows) * 1.3 + 1)
        fig.text(.5, .025, "Fixed candidate stream: no evidence about prompt quality or real LLM duplicate rates."
                 if study["phase"] == "offline_integration" else "Inspect raw runs and costs before interpreting differences.",
                 ha="center", fontsize=8)
        fig.savefig(output / "Figure_2.png", bbox_inches="tight")
        fig.savefig(output / "Figure_2.pdf", bbox_inches="tight")
        plt.close(fig)
    print(f"Figures and summary saved under {root.resolve()}")


if __name__ == "__main__":
    main()
