#!/usr/bin/env python3
import argparse
import json
from pathlib import Path
from typing import Any, Dict, List, Tuple

import numpy as np
import matplotlib.pyplot as plt
from matplotlib.patches import Patch


def load_ablations(path: Path) -> Dict[str, Any]:
    j = json.loads(path.read_text())
    # Both files store ablations under "ablations_caseB" (historical naming).
    return j.get("ablations_caseB", {})


def get_vals(abl: Dict[str, Any], keys: List[str]) -> List[float]:
    out: List[float] = []
    for k in keys:
        try:
            out.append(float(abl.get(k, {}).get("stuck_pct", 0.0)))
        except Exception:
            out.append(0.0)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--caseb", default="results_next/report_results.json", help="JSON containing Case-B ablations")
    ap.add_argument("--harsh", default="results_next/report_results_harsh.json", help="JSON containing harsher tc ablations")
    ap.add_argument("--out", default="Figures/2. Result/ablation_compare.png")
    ap.add_argument("--sensitive-thresh", type=float, default=0.1, help="Threshold percent for highlighting sensitive mechanisms")
    args = ap.parse_args()

    repo = Path(__file__).resolve().parents[1]
    caseb_path = repo / args.caseb
    harsh_path = repo / args.harsh
    out_path = repo / args.out
    out_path.parent.mkdir(parents=True, exist_ok=True)

    keys = ["full", "udp_only_no_snapshots", "no_watchdog", "no_deadline", "quic_only_edges"]
    labels = ["Full", "UDP-only", "No watchdog", "No deadline", "QUIC-only edges"]

    abl_b = load_ablations(caseb_path) if caseb_path.exists() else {}
    abl_h = load_ablations(harsh_path) if harsh_path.exists() else {}

    v_b = np.asarray(get_vals(abl_b, keys), dtype=float)
    v_h = np.asarray(get_vals(abl_h, keys), dtype=float)

    # Sensitive = mechanisms in harsh regime with non-trivial stuck rate.
    sensitive_mask = v_h >= float(args.sensitive_thresh)

    plt.figure(figsize=(8.4, 3.8), dpi=200)
    x = np.arange(len(keys))
    w = 0.36

    b1 = plt.bar(x - w / 2, v_b, width=w, label="Case B (moderate)")
    b2 = plt.bar(x + w / 2, v_h, width=w, label="Harsh tc netem")

    # Overlay hatch on harsh bars that are "sensitive" under harsh regime.
    for i, (bar, is_sel) in enumerate(zip(b2, sensitive_mask)):
        if not is_sel:
            continue
        plt.bar(
            bar.get_x() + bar.get_width() / 2,
            v_h[i],
            width=w,
            color="none",
            edgecolor="black",
            hatch="///",
            linewidth=0.0,
        )

    plt.xticks(x, labels, rotation=18, ha="right")
    plt.ylabel("Stuck rate (%)")
    plt.title("Stuck-rate ablation comparison (Case B vs harsh)")
    plt.grid(True, axis="y", alpha=0.35)

    # Value labels
    for bars, vals in [(b1, v_b), (b2, v_h)]:
        for bar, v in zip(bars, vals):
            plt.text(bar.get_x() + bar.get_width() / 2, v + 0.15, f"{v:.1f}%", ha="center", va="bottom", fontsize=7)

    legend_extra = Patch(facecolor="none", edgecolor="black", hatch="///", label=f"Highlighted: harsh ≥ {args.sensitive_thresh:.1f}%")
    plt.legend(handles=[b1, b2, legend_extra], fontsize=8, loc="upper left")

    plt.tight_layout()
    plt.savefig(out_path)
    plt.close()
    print(f"Wrote {out_path}")


if __name__ == "__main__":
    main()


