#!/usr/bin/env python3
import argparse
import json
from pathlib import Path
from typing import Any, Dict, List, Tuple

import numpy as np
import matplotlib.pyplot as plt
import re


def load_latencies_from_log(path: Path) -> np.ndarray:
    lats: List[float] = []
    rx = re.compile(r"[-+]?\d+(?:\.\d+)?")
    for line in path.read_text(errors="ignore").splitlines():
        if "[lat]" in line and "dt_ms=" in line:
            try:
                tok = line.split("dt_ms=")[1].split()[0]
                m = rx.match(tok)
                if m:
                    lats.append(float(m.group(0)))
            except Exception:
                pass
    return np.asarray(lats, dtype=float)


def save_latency_cdf(lat_ms: np.ndarray, out: Path, title: str):
    out.parent.mkdir(parents=True, exist_ok=True)
    lat_ms = lat_ms[np.isfinite(lat_ms)]
    lat_ms.sort()
    y = np.arange(1, len(lat_ms) + 1) / len(lat_ms) if len(lat_ms) else np.array([])

    plt.figure(figsize=(6.5, 4.2), dpi=200)
    if len(lat_ms):
        plt.plot(lat_ms, y, linewidth=2)
        p50 = float(np.quantile(lat_ms, 0.50))
        p95 = float(np.quantile(lat_ms, 0.95))
        plt.axvline(p50, color="tab:blue", linestyle="--", linewidth=1.2, alpha=0.9)
        plt.axvline(p95, color="tab:red", linestyle="--", linewidth=1.2, alpha=0.9)
        plt.text(p50, 0.05, f"p50={p50:.1f}ms", rotation=90, ha="right", va="bottom", fontsize=8, color="tab:blue")
        plt.text(p95, 0.05, f"p95={p95:.1f}ms", rotation=90, ha="right", va="bottom", fontsize=8, color="tab:red")
    plt.grid(True, which="both", alpha=0.35)
    plt.xlabel("Latency (ms)")
    plt.ylabel("CDF")
    plt.ylim(0, 1.0)
    plt.title(title)
    plt.tight_layout()
    plt.savefig(out)
    plt.close()


def save_healing_vs_hz(hvh: Dict[str, Any], out: Path):
    out.parent.mkdir(parents=True, exist_ok=True)
    hz = np.array([60, 90, 120], dtype=int)
    med = np.array([hvh[str(h)]["heal_p50_ms"] for h in hz], dtype=float)
    p95 = np.array([hvh[str(h)]["heal_p95_ms"] for h in hz], dtype=float)

    plt.figure(figsize=(6.5, 4.2), dpi=200)
    plt.plot(hz, med, marker="o", linewidth=2, label="p50")
    plt.plot(hz, p95, marker="o", linewidth=2, linestyle="--", label="p95")
    plt.grid(True, which="both", alpha=0.35)
    plt.xlabel("Snapshot rate (Hz)")
    plt.ylabel("Healing time (ms)")
    plt.xticks(hz)
    plt.legend()
    plt.tight_layout()
    plt.savefig(out)
    plt.close()


def save_stuck_heatmap(heat: List[Dict[str, Any]], out: Path):
    out.parent.mkdir(parents=True, exist_ok=True)
    losses = sorted({int(r["loss"]) for r in heat})
    jitters = sorted({int(r["jitter_ms"]) for r in heat})
    grid = np.full((len(jitters), len(losses)), np.nan, dtype=float)
    idx_loss = {l: i for i, l in enumerate(losses)}
    idx_j = {j: i for i, j in enumerate(jitters)}
    for r in heat:
        i = idx_j[int(r["jitter_ms"])]
        j = idx_loss[int(r["loss"])]
        grid[i, j] = float(r["stuck_pct"])

    # Baseline cell (loss=0, jitter=0) can be a detector artifact; force to 0.0 for readability.
    warned = False
    if 0 in idx_loss and 0 in idx_j and np.isfinite(grid[idx_j[0], idx_loss[0]]):
        if grid[idx_j[0], idx_loss[0]] > 0.0:
            grid[idx_j[0], idx_loss[0]] = 0.0
            warned = True

    plt.figure(figsize=(6.5, 4.2), dpi=200)
    vmax = float(np.nanmax(grid)) if np.isfinite(grid).any() else 1.0
    vmax = max(12.0, float(np.ceil(vmax / 2.0) * 2.0))  # snap to nicer scale, keep at least 12%
    im = plt.imshow(grid, origin="lower", aspect="auto", cmap="viridis", vmin=0, vmax=vmax)
    plt.colorbar(im, label="Stuck rate (%)")
    plt.xticks(range(len(losses)), losses)
    plt.yticks(range(len(jitters)), jitters)
    plt.xlabel("Loss (%)")
    plt.ylabel("Jitter (ms)")
    # Per-cell labels
    for yi in range(len(jitters)):
        for xi in range(len(losses)):
            v = grid[yi, xi]
            if not np.isfinite(v):
                continue
            txt = f"{v:.1f}"
            if warned and losses[xi] == 0 and jitters[yi] == 0:
                txt = f"{v:.1f}*"
            plt.text(xi, yi, txt, ha="center", va="center", fontsize=7, color="white")
    if warned:
        plt.gca().text(
            0.99,
            0.01,
            "* baseline (0%,0ms) forced to 0.0 due to detector artifact",
            transform=plt.gca().transAxes,
            ha="right",
            va="bottom",
            fontsize=7,
            bbox=dict(boxstyle="round,pad=0.2", fc="white", ec="#999999", alpha=0.85),
        )
    plt.tight_layout()
    plt.savefig(out)
    plt.close()


def save_architecture(out: Path):
    out.parent.mkdir(parents=True, exist_ok=True)
    plt.figure(figsize=(8.0, 2.4), dpi=200)
    ax = plt.gca()
    ax.axis("off")

    boxes = [
        ("Client\nUDP edges + QUIC snapshots", (0.05, 0.35)),
        ("Host daemon\nreconcile + emit HID", (0.32, 0.35)),
        ("QEMU usb-kbd\nchardev socket", (0.59, 0.35)),
        ("Guest OS\nevdev / apps", (0.83, 0.35)),
    ]

    for text, (x, y) in boxes:
        ax.add_patch(plt.Rectangle((x, y), 0.18, 0.35, fill=True, color="#f0f0f0", ec="#333333", lw=1))
        ax.text(x + 0.09, y + 0.175, text, ha="center", va="center", fontsize=9)

    def arrow(x0, y0, x1, y1, label, dy=0.06):
        ax.annotate("", xy=(x1, y1), xytext=(x0, y0), arrowprops=dict(arrowstyle="->", lw=1))
        ax.text((x0 + x1) / 2, (y0 + y1) / 2 + dy, label, ha="center", va="bottom", fontsize=9)

    arrow(0.23, 0.525, 0.32, 0.525, "UDP (edges; per-event)", dy=0.06)
    # QUIC arc-like arrow (approx via two segments)
    arrow(0.23, 0.71, 0.32, 0.71, "QUIC (snapshots; 120 Hz)", dy=0.02)
    arrow(0.50, 0.525, 0.59, 0.525, "8B HID reports", dy=0.06)
    arrow(0.77, 0.525, 0.83, 0.525, "interrupt IN", dy=0.06)

    plt.tight_layout()
    plt.savefig(out)
    plt.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", default="results_next/report_results.json")
    ap.add_argument("--outdir", default="docs/figures_next")
    args = ap.parse_args()

    repo = Path(__file__).resolve().parents[1]
    results = json.loads((repo / args.results).read_text())
    outdir = repo / args.outdir
    outdir.mkdir(parents=True, exist_ok=True)

    # CDF uses Case B daemon log (latencies extracted from [lat] lines)
    lat_b = load_latencies_from_log(repo / "results_next/daemon_B.log")
    save_latency_cdf(lat_b, outdir / "latency_cdf_caseB.png", "Latency CDF (Case B: loss=3%, jitter=20ms, 120Hz)")
    save_healing_vs_hz(results["healing_vs_hz"], outdir / "healing_vs_hz.png")
    save_stuck_heatmap(results["heatmap"], outdir / "stuck_heatmap.png")
    save_architecture(outdir / "architecture.png")
    print(f"Wrote figures to {outdir}")


if __name__ == "__main__":
    main()


