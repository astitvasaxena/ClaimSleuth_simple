"""Draw boxplot_latency.png and heatmap_agent_latency.png from the experiment CSVs (grey/blue, readable).

Run with the tools venv (matplotlib is not part of the product venvs):
    <tools-venv>/bin/python docs/submission/results/make_plots.py
Reads experiment_runs.csv next to this file; draws nothing it cannot read from there.
"""
from __future__ import annotations

import csv
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

HERE = Path(__file__).resolve().parent
BLUE, GREY, DARK = "#2f6db5", "#9aa5b1", "#1f2933"
LABELS = {"seq_direct": "Sequential-only", "par_direct": "Parallel-only\n(no sharing)",
          "p2p_direct": "Direct message\npassing", "hier_blackboard": "Hierarchical\n+ blackboard"}


def load() -> list[dict]:
    with (HERE / "experiment_runs.csv").open(encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    for r in rows:
        for k in ("wall_ms", "evidence_ms", "investigator_ms", "forensic_ms"):
            r[k] = float(r[k]) if r[k] not in ("", None) else None
    return [r for r in rows if r["wall_ms"] is not None and not r["run_error"]]


def style(ax) -> None:
    ax.set_facecolor("white")
    ax.grid(axis="y", color="#d9dee3", linewidth=0.8)
    ax.set_axisbelow(True)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)


def box(ax, groups: dict[str, list[float]], title: str, colour: str) -> None:
    names = list(groups)
    data = [[v / 1000 for v in groups[n]] for n in names]
    bp = ax.boxplot(data, patch_artist=True, widths=0.55, showmeans=True,
                    meanprops=dict(marker="D", markerfacecolor="white", markeredgecolor=DARK, markersize=5),
                    medianprops=dict(color=DARK, linewidth=1.6), flierprops=dict(marker="o", markersize=3, markerfacecolor=GREY,
                                                                              markeredgecolor=GREY))
    for patch in bp["boxes"]:
        patch.set(facecolor=colour, alpha=0.55, edgecolor=DARK)
    ax.set_xticks(range(1, len(names) + 1), [f"{n}\n(n={len(groups[n])})" for n in names], fontsize=9)
    ax.set_ylabel("System latency per claim (s)")
    ax.set_title(title, fontsize=11, color=DARK, loc="left")
    style(ax)


def main() -> None:
    rows = load()
    std40 = [r for r in rows if r["phase"] == "eval40" and r["structure"] == "standard"]
    arch = {LABELS[c].replace("\n", " "): [r["wall_ms"] for r in std40 if r["config"] == c]
            for c in ("seq_direct", "par_direct", "p2p_direct", "hier_blackboard")}
    e5 = [r for r in rows if r["config"] == "hier_blackboard"]
    persona = {}
    for p in ("honest", "vague", "abusive", "injection"):
        for s, tag in (("standard", "std"), ("evidence_first_react", "ReAct")):
            vals = [r["wall_ms"] for r in e5 if r["persona"] == p and r["structure"] == s]
            if vals:
                persona[f"{p}\n{tag}"] = vals
    fig, axes = plt.subplots(1, 2, figsize=(14, 5.2), gridspec_kw={"width_ratios": [1, 1.25]})
    box(axes[0], {k.replace(" ", "\n", 1): v for k, v in arch.items()}, "E3/E4: wiring (40 cases each)", BLUE)
    box(axes[1], persona, "E5: persona x prompt structure (default wiring)", GREY)
    fig.suptitle("ClaimSleuth system latency per claim, OFFLINE (labelled test doubles, not LLM reasoning)",
                 fontsize=12, color=DARK)
    fig.tight_layout(rect=(0, 0, 1, 0.94))
    fig.savefig(HERE / "boxplot_latency.png", dpi=160)
    plt.close(fig)

    # heat map: mean latency (ms) per agent x persona (default wiring, standard structure)
    cols = ["evidence_ms", "investigator_ms", "forensic_ms", "wall_ms"]
    names = ["Evidence", "Investigator", "Forensic", "System (wall)"]
    personas = [p for p in ("honest", "vague", "abusive", "injection") if any(r["persona"] == p for r in e5)]
    grid = np.array([[np.mean([r[c] for r in e5 if r["persona"] == p and r["structure"] == "standard" and r[c] is not None])
                      for p in personas] for c in cols])
    fig, ax = plt.subplots(figsize=(8.2, 4.4))
    im = ax.imshow(grid, cmap="Blues", aspect="auto")
    ax.set_xticks(range(len(personas)), [f"{p}\n(n={sum(1 for r in e5 if r['persona'] == p and r['structure'] == 'standard')})"
                                         for p in personas])
    ax.set_yticks(range(len(names)), names)
    for i in range(grid.shape[0]):
        for j in range(grid.shape[1]):
            ax.text(j, i, f"{grid[i, j]:.0f}", ha="center", va="center", fontsize=11,
                    color="white" if grid[i, j] > grid.max() * 0.6 else DARK)
    cb = fig.colorbar(im, ax=ax)
    cb.set_label("mean latency (ms)")
    ax.set_title("Mean latency per agent and persona, OFFLINE test doubles", fontsize=11, color=DARK, loc="left")
    fig.tight_layout()
    fig.savefig(HERE / "heatmap_agent_latency.png", dpi=160)
    plt.close(fig)
    print("wrote boxplot_latency.png and heatmap_agent_latency.png")


if __name__ == "__main__":
    main()
