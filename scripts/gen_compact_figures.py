#!/usr/bin/env python3
import argparse
import json
from pathlib import Path
from typing import Any, Dict, List, Tuple

import numpy as np
import matplotlib.pyplot as plt
import re


def load_vals_from_log(path: Path, key: str) -> np.ndarray:
    vals: List[float] = []
    rx = re.compile(r"[-+]?\d+(?:\.\d+)?")
    for line in path.read_text(errors="ignore").splitlines():
        if key not in line:
            continue
        try:
            s = line.split(key, 1)[1].strip()
            tok = s.split()[0]
            m = rx.match(tok)
            if m:
                vals.append(float(m.group(0)))
        except Exception:
            pass
    return np.asarray(vals, dtype=float)


def ecdf(xs: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
    xs = xs[np.isfinite(xs)]
    xs.sort()
    if xs.size == 0:
        return xs, np.array([])
    y = np.arange(1, xs.size + 1) / xs.size
    return xs, y


def save_compact_bars(results: Dict[str, Any], out: Path):
    """
    One figure containing:
      (a) ablation bars (Case B)
      (b) ablation bars (Harsh, if provided)
      (c) bandwidth breakdown (stacked)
    """
    out.parent.mkdir(parents=True, exist_ok=True)
    fig, axes = plt.subplots(1, 3, figsize=(12.0, 3.4), dpi=200)

    keys = ["full", "udp_only_no_snapshots", "no_watchdog", "no_deadline", "quic_only_edges"]
    labels = ["Full", "UDP-only", "No watchdog", "No deadline", "QUIC-only"]

    def ablation_vals(abl: Dict[str, Any]) -> List[float]:
        return [float(abl.get(k, {}).get("stuck_pct", 0.0)) for k in keys]

    # (a) Case-B ablation (from current results file)
    ablB = results.get("ablations_caseB", {})
    vB = ablation_vals(ablB)
    ax = axes[0]
    x = np.arange(len(keys))
    ax.bar(x, vB)
    ax.set_xticks(x, labels, rotation=20, ha="right")
    ax.set_ylabel("Stuck rate (%)")
    ax.set_title("Ablation (Case B)")
    ax.grid(True, axis="y", alpha=0.35)
    for i, v in enumerate(vB):
        ax.text(i, v + 0.2, f"{v:.1f}%", ha="center", va="bottom", fontsize=7)

    # (b) Harsh ablation (optional)
    ax = axes[1]
    harsh = results.get("ablations_harsh", None)
    if isinstance(harsh, dict) and harsh:
        vH = ablation_vals(harsh)
        ax.bar(x, vH)
        ax.set_xticks(x, labels, rotation=20, ha="right")
        ax.set_title("Ablation (Harsh tc)")
        ax.grid(True, axis="y", alpha=0.35)
        for i, v in enumerate(vH):
            ax.text(i, v + 0.2, f"{v:.1f}%", ha="center", va="bottom", fontsize=7)
    else:
        ax.axis("off")
        ax.text(0.5, 0.5, "Harsh ablation\nnot provided", ha="center", va="center")

    # (c) Bandwidth breakdown stacked bars
    meta = results.get("meta", {})
    repeat = int(meta.get("repeat", 100))
    key_delay_ms = float(meta.get("key_delay_ms", 10.0))
    total_markers = 4 * repeat
    edges_per_char = 3
    edge_bytes = total_markers * edges_per_char * 28
    per_char_ms = key_delay_ms + key_delay_ms + 1.0
    seconds = (4 * repeat * per_char_ms) / 1000.0 + 1.5
    hz = np.array([60, 90, 120], dtype=int)
    snap_bytes = hz * seconds * 52.0
    edge_kbps = edge_bytes * 8 / seconds / 1000.0
    snap_kbps = snap_bytes * 8 / seconds / 1000.0

    ax = axes[2]
    x2 = np.arange(len(hz))
    b1 = ax.bar(x2, [edge_kbps] * len(hz), label="Edges (UDP)", alpha=0.9)
    b2 = ax.bar(x2, snap_kbps, bottom=[edge_kbps] * len(hz), label="Snapshots (QUIC)", alpha=0.9)
    ax.set_xticks(x2, [str(h) for h in hz])
    ax.set_title("Bandwidth breakdown")
    ax.set_ylabel("kb/s (est.)")
    ax.grid(True, axis="y", alpha=0.35)
    ax.legend(handles=[b1, b2], fontsize=7, loc="upper left")

    fig.tight_layout()
    fig.savefig(out)
    plt.close(fig)


def save_healing_dualaxis(repo: Path, results: Dict[str, Any], out: Path, threshold_ms: float = 100.0):
    """
    Combine healing-vs-Hz (p50/p95) with a second y-axis showing P(heal <= threshold_ms),
    derived from the per-Hz heal logs (ECDF at a fixed threshold).
    """
    out.parent.mkdir(parents=True, exist_ok=True)

    hvh = results.get("healing_vs_hz", {})
    wanted = [60, 90, 120]
    hz_list = [h for h in wanted if str(h) in hvh]
    hz = np.array(hz_list if hz_list else sorted(int(k) for k in hvh.keys()), dtype=int)
    p50 = np.array([float(hvh[str(h)]["heal_p50_ms"]) for h in hz], dtype=float)
    p95 = np.array([float(hvh[str(h)]["heal_p95_ms"]) for h in hz], dtype=float)

    # Derive probability from logs if present; otherwise leave NaN.
    probs: List[float] = []
    for h in hz:
        p = repo / f"results_next/daemon_heal_{h}.log"
        if not p.exists():
            probs.append(float("nan"))
            continue
        heals = load_vals_from_log(p, "heal_ms=")
        heals = heals[np.isfinite(heals)]
        if heals.size == 0:
            probs.append(float("nan"))
        else:
            probs.append(float(np.mean(heals <= threshold_ms)))

    probs_arr = np.asarray(probs, dtype=float)

    fig, ax1 = plt.subplots(figsize=(7.2, 3.8), dpi=200)
    l1 = ax1.plot(hz, p50, marker="o", linewidth=2, label="p50 (ms)")
    l2 = ax1.plot(hz, p95, marker="o", linewidth=2, linestyle="--", label="p95 (ms)")
    # Theoretical bound line: T_snapshot + T_USB_poll (approx 8ms at 125Hz polling).
    usb_poll_ms = 8.0
    bound = (1000.0 / hz) + usb_poll_ms
    l0 = ax1.plot(hz, bound, color="black", linewidth=1.2, linestyle=":", label=r"Bound: $1000/\mathrm{Hz} + 8$ ms")
    ax1.set_xlabel("Snapshot rate (Hz)")
    ax1.set_ylabel("Healing time (ms)")
    ax1.grid(True, which="both", alpha=0.35)
    ax1.set_xticks(hz)

    ax2 = ax1.twinx()
    l3 = ax2.plot(hz, probs_arr * 100.0, marker="s", linewidth=2, color="tab:green", label=f"P(heal ≤ {threshold_ms:.0f}ms) (%)")
    ax2.set_ylabel("Probability (%)")
    ax2.set_ylim(0, 100)

    # Shared legend
    lines = l1 + l2 + l0 + l3
    labels = [ln.get_label() for ln in lines]
    ax1.legend(lines, labels, fontsize=8, loc="center right")
    ax1.set_title("Healing vs snapshot rate + tail proxy (dual-axis)")
    # Small explanation callout for flat tails.
    ax1.text(
        0.02,
        0.98,
        "Note: p95 can be dominated by\nphase alignment + jitter tails",
        transform=ax1.transAxes,
        ha="left",
        va="top",
        fontsize=7,
        bbox=dict(boxstyle="round,pad=0.25", fc="white", ec="#999999", alpha=0.85),
    )

    fig.tight_layout()
    fig.savefig(out)
    plt.close(fig)


def copy_required(repo: Path, dest_dir: Path, mapping: List[Tuple[str, str]]):
    dest_dir.mkdir(parents=True, exist_ok=True)
    for src_rel, dst_name in mapping:
        src = repo / src_rel
        if not src.exists():
            continue
        (dest_dir / dst_name).write_bytes(src.read_bytes())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", default="results_next/report_results.json")
    ap.add_argument("--outdir", default="Figures/2. Result")
    ap.add_argument("--threshold-ms", type=float, default=100.0)
    args = ap.parse_args()

    repo = Path(__file__).resolve().parents[1]
    results = json.loads((repo / args.results).read_text())
    outdir = repo / args.outdir
    outdir.mkdir(parents=True, exist_ok=True)

    # Compact composite figures
    save_compact_bars(results, outdir / "bars_compact.png")
    save_healing_dualaxis(repo, results, outdir / "healing_dualaxis.png", threshold_ms=args.threshold_ms)

    # Copy other figures already generated into the requested directory.
    copy_required(repo, outdir, [
        ("docs/figures_next/cdf_overlay_AD.png", "cdf_overlay_AD.png"),
        ("docs/figures_next/cdf_baselines_caseB.png", "cdf_baselines_caseB.png"),
        ("docs/figures_next/latency_cdf_caseB.png", "latency_cdf_caseB.png"),
        ("docs/figures_next/stuck_heatmap.png", "stuck_heatmap.png"),
        ("docs/figures_next/architecture.png", "architecture.png"),
    ])

    print(f"Wrote compact figures and copied assets to {outdir}")


if __name__ == "__main__":
    main()


