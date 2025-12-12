#!/usr/bin/env python3
import argparse
from pathlib import Path

import matplotlib.pyplot as plt
import matplotlib.image as mpimg
from matplotlib.patches import Patch


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--caseb", default="docs/figures_next/ablation_stuck_bars_all5_caseB.png")
    ap.add_argument("--harsh_all5", default="docs/figures_next/ablation_stuck_bars_all5_harsh.png")
    ap.add_argument("--harsh_sensitive", default="docs/figures_next/ablation_stuck_bars_sensitive_harsh.png")
    ap.add_argument("--out", default="Figures/2. Result/ablation_triptych.png")
    args = ap.parse_args()

    repo = Path(__file__).resolve().parents[1]
    p_caseb = repo / args.caseb
    p_harsh_all5 = repo / args.harsh_all5
    p_harsh_sens = repo / args.harsh_sensitive
    out = repo / args.out
    out.parent.mkdir(parents=True, exist_ok=True)

    imgs = [
        ("CaseB_all5", p_caseb),
        ("Harsh_all5", p_harsh_all5),
        ("Harsh_sensitive", p_harsh_sens),
    ]
    loaded = [(name, mpimg.imread(str(p))) for name, p in imgs]

    fig, axes = plt.subplots(1, 3, figsize=(13.2, 3.6), dpi=200)
    for ax, (name, im) in zip(axes, loaded):
        ax.imshow(im)
        ax.axis("off")

    # Legend: label panels, not bar colors (bars are single-color).
    handles = [
        Patch(facecolor="#1f77b4", edgecolor="none", label="Case B (all 5 toggles)"),
        Patch(facecolor="#ff7f0e", edgecolor="none", label="Harsh tc regime (all 5 toggles)"),
        Patch(facecolor="#2ca02c", edgecolor="none", label="Harsh tc regime (sensitive-only view)"),
    ]
    fig.legend(handles=handles, loc="upper center", ncol=3, fontsize=9, frameon=True)
    fig.suptitle("Stuck-rate ablations across regimes (compact triptych)", y=1.02, fontsize=12)
    fig.tight_layout()
    fig.savefig(out, bbox_inches="tight")
    plt.close(fig)
    print(f"Wrote {out}")


if __name__ == "__main__":
    main()


