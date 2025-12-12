#!/usr/bin/env python3
import argparse
import json
from pathlib import Path
from typing import Any, Dict, List, Tuple

import numpy as np
import matplotlib.pyplot as plt
import re


def load_vals_from_log(path: Path, key: str, *, must_contain: str = "") -> np.ndarray:
    vals: List[float] = []
    # Robust float prefix parsing (handles negatives and avoids concatenated tokens).
    rx = re.compile(r"[-+]?\d+(?:\.\d+)?")
    for line in path.read_text(errors="ignore").splitlines():
        if must_contain and must_contain not in line:
            continue
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


def _q(lat: np.ndarray, q: float) -> float:
    lat = lat[np.isfinite(lat)]
    if lat.size == 0:
        return float("nan")
    return float(np.quantile(lat, q))


def save_cdf_overlay(
    lat_by_label: List[Tuple[str, np.ndarray]],
    out: Path,
    title: str,
    *,
    show_p99: bool = False,
    stuck_pct_by_label: Dict[str, float] | None = None,
    summary_by_label: Dict[str, Dict[str, float]] | None = None,
):
    out.parent.mkdir(parents=True, exist_ok=True)
    plt.figure(figsize=(6.8, 4.4), dpi=200)
    for label, lat in lat_by_label:
        x, y = ecdf(lat)
        if x.size:
            p50 = _q(lat, 0.50)
            p95 = _q(lat, 0.95)
            p99 = _q(lat, 0.99)
            legend = f"{label} (p50={p50:.1f}ms, p95={p95:.1f}ms"
            if show_p99:
                legend += f", p99={p99:.1f}ms"
            legend += ")"
            plt.plot(x, y, linewidth=1.6, label=legend)

            # Optional: lightweight annotation at (p95, 0.95) to make p95 visible even with long legends.
            if np.isfinite(p95):
                plt.scatter([p95], [0.95], s=10)
        else:
            # No raw samples (e.g., log overwritten). If summary stats exist, show percentile markers.
            if summary_by_label and label in summary_by_label:
                s = summary_by_label[label]
                p50 = float(s.get("p50_ms", float("nan")))
                p95 = float(s.get("p95_ms", float("nan")))
                p99 = float(s.get("p99_ms", float("nan")))
                legend = f"{label} (p50={p50:.1f}ms, p95={p95:.1f}ms"
                if show_p99:
                    legend += f", p99={p99:.1f}ms"
                legend += "; markers only)"
                xs = []
                ys = []
                if np.isfinite(p50):
                    xs.append(p50); ys.append(0.50)
                if np.isfinite(p95):
                    xs.append(p95); ys.append(0.95)
                if np.isfinite(p99):
                    xs.append(p99); ys.append(0.99)
                if xs:
                    plt.plot(xs, ys, marker="o", linestyle="none", label=legend)
    plt.grid(True, which="both", alpha=0.35)
    plt.xlabel("Latency (ms)")
    plt.ylabel("CDF")
    plt.ylim(0, 1.0)
    plt.title(title)
    plt.legend(fontsize=8, loc="upper left")

    # Optional correctness callout (keeps CDF latency-only but surfaces stuck rates).
    if stuck_pct_by_label:
        lines = ["Latency only; stuck% shown:"]
        for k, v in stuck_pct_by_label.items():
            lines.append(f"- {k}: {v:.1f}%")
        plt.gca().text(
            0.02,
            0.02,
            "\n".join(lines),
            transform=plt.gca().transAxes,
            ha="left",
            va="bottom",
            fontsize=7,
            bbox=dict(boxstyle="round,pad=0.25", fc="white", ec="#999999", alpha=0.85),
        )
    plt.tight_layout()
    plt.savefig(out)
    plt.close()


def save_ablation_bars_all5(abl: Dict[str, Any], out: Path, title: str):
    out.parent.mkdir(parents=True, exist_ok=True)
    keys = ["full", "udp_only_no_snapshots", "no_watchdog", "no_deadline", "quic_only_edges"]
    labels = ["Full", "UDP-only", "No watchdog", "No deadline", "QUIC-only edges"]
    vals = [float(abl[k]["stuck_pct"]) if k in abl else 0.0 for k in keys]

    plt.figure(figsize=(6.8, 3.6), dpi=200)
    x = np.arange(len(vals))
    plt.bar(x, vals)
    plt.xticks(x, labels, rotation=18, ha="right")
    plt.ylabel("Stuck rate (%)")
    plt.grid(True, axis="y", alpha=0.35)
    for i, v in enumerate(vals):
        plt.text(i, v + 0.25, f"{v:.1f}%", ha="center", va="bottom", fontsize=8)
    plt.title(title)
    plt.tight_layout()
    plt.savefig(out)
    plt.close()


def save_ablation_bars_sensitive(abl: Dict[str, Any], out: Path, title: str):
    out.parent.mkdir(parents=True, exist_ok=True)
    # Show only baselines + any ablation that produces non-trivial stuck.
    candidates: List[Tuple[str, str]] = [
        ("full", "Full"),
        ("udp_only_no_snapshots", "UDP-only"),
        ("no_deadline", "No deadline"),
        ("no_watchdog", "No watchdog"),
        ("quic_only_edges", "QUIC-only edges"),
    ]
    selected: List[Tuple[str, str, float]] = []
    for k, label in candidates:
        if k not in abl:
            continue
        v = float(abl[k].get("stuck_pct", 0.0))
        keep = (k in {"full", "udp_only_no_snapshots", "quic_only_edges"}) or (v >= 0.1)
        if keep:
            selected.append((k, label, v))

    labels = [t[1] for t in selected]
    vals = [t[2] for t in selected]

    plt.figure(figsize=(6.8, 3.6), dpi=200)
    x = np.arange(len(vals))
    plt.bar(x, vals)
    plt.xticks(x, labels, rotation=18, ha="right")
    plt.ylabel("Stuck rate (%)")
    plt.grid(True, axis="y", alpha=0.35)
    for i, v in enumerate(vals):
        plt.text(i, v + 0.25, f"{v:.1f}%", ha="center", va="bottom", fontsize=8)
    plt.title(title)
    plt.tight_layout()
    plt.savefig(out)
    plt.close()


def save_bandwidth_breakdown(meta: Dict[str, Any], out: Path):
    """
    Stacked bars: estimated edge vs snapshot bandwidth at different snapshot Hz
    (using the same assumptions as the runner).
    """
    out.parent.mkdir(parents=True, exist_ok=True)
    repeat = int(meta.get("repeat", 100))
    key_delay_ms = float(meta.get("key_delay_ms", 10.0))

    # Use apt workload for baseline rate assumptions (4 chars: a,p,t,ENTER).
    total_markers = 4 * repeat
    edges_per_char = 3
    edge_bytes = total_markers * edges_per_char * 28

    # Duration estimate mirrors scripts/run_report_experiments.py.
    per_char_ms = key_delay_ms + key_delay_ms + 1.0
    seconds = (4 * repeat * per_char_ms) / 1000.0 + 1.5

    hz = np.array([60, 90, 120], dtype=int)
    snap_bytes = hz * seconds * 52.0
    edge_kbps = edge_bytes * 8 / seconds / 1000.0
    snap_kbps = snap_bytes * 8 / seconds / 1000.0

    plt.figure(figsize=(6.8, 3.6), dpi=200)
    x = np.arange(len(hz))
    plt.bar(x, [edge_kbps] * len(hz), label="Edges (UDP)", alpha=0.9)
    plt.bar(x, snap_kbps, bottom=[edge_kbps] * len(hz), label="Snapshots (QUIC)", alpha=0.9)
    plt.xticks(x, [str(h) for h in hz])
    plt.xlabel("Snapshot rate (Hz)")
    plt.ylabel("Estimated bandwidth (kb/s)")
    plt.grid(True, axis="y", alpha=0.35)
    plt.legend(fontsize=8)
    plt.tight_layout()
    plt.savefig(out)
    plt.close()


def save_heal_ecdf(repo: Path, out: Path):
    out.parent.mkdir(parents=True, exist_ok=True)
    logs = [
        ("60 Hz", repo / "results_next/daemon_heal_60.log"),
        ("90 Hz", repo / "results_next/daemon_heal_90.log"),
        ("120 Hz", repo / "results_next/daemon_heal_120.log"),
    ]
    plt.figure(figsize=(6.8, 4.4), dpi=200)
    for label, p in logs:
        if not p.exists():
            continue
        heals = load_vals_from_log(p, "heal_ms=", must_contain="[heal]")
        x, y = ecdf(heals)
        if x.size:
            plt.plot(x, y, linewidth=1.6, label=label)
    plt.grid(True, which="both", alpha=0.35)
    plt.xlabel("Healing time (ms)")
    plt.ylabel("ECDF")
    plt.ylim(0, 1.0)
    plt.title("Healing time ECDF (forced missing keyup)")
    plt.legend(fontsize=8)
    plt.tight_layout()
    plt.savefig(out)
    plt.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", default="results_next/report_results.json")
    ap.add_argument("--outdir", default="docs/figures_next")
    ap.add_argument("--tag", default="", help="Optional suffix for filenames (e.g., 'harsh').")
    args = ap.parse_args()

    repo = Path(__file__).resolve().parents[1]
    results = json.loads((repo / args.results).read_text())
    outdir = repo / args.outdir
    outdir.mkdir(parents=True, exist_ok=True)

    # Overlaid CDFs for Cases A–D.
    lat_by = []
    for c in ["A", "B", "C", "D"]:
        p = repo / f"results_next/daemon_{c}.log"
        if p.exists():
            lat_by.append((f"Case {c}", load_vals_from_log(p, "dt_ms=", must_contain="[lat]")))
    save_cdf_overlay(lat_by, outdir / "cdf_overlay_AD.png", "Latency CDF overlay (Cases A–D)")

    # Baselines/ablations CDF overlay (Case B settings).
    base_lat = []
    for label, fn in [
        ("Full (hybrid)", "daemon_abl_full.log"),
        ("UDP-only (no snapshots)", "daemon_abl_udp_only.log"),
        ("QUIC-only edges", "daemon_abl_quic_only.log"),
    ]:
        p = repo / f"results_next/{fn}"
        if p.exists():
            base_lat.append((label, load_vals_from_log(p, "dt_ms=", must_contain="[lat]")))
    abl = results.get("ablations_caseB", {})
    stuck_callout = {
        "Full": float(abl.get("full", {}).get("stuck_pct", 0.0)),
        "UDP-only": float(abl.get("udp_only_no_snapshots", {}).get("stuck_pct", 0.0)),
        "QUIC-only": float(abl.get("quic_only_edges", {}).get("stuck_pct", 0.0)),
    }
    save_cdf_overlay(
        base_lat,
        outdir / "cdf_baselines_caseB.png",
        "Baseline latency CDFs (Case B)",
        show_p99=True,
        stuck_pct_by_label=stuck_callout,
        summary_by_label={
            "QUIC-only edges": {
                "p50_ms": float(abl.get("quic_only_edges", {}).get("p50_ms", float("nan"))),
                "p95_ms": float(abl.get("quic_only_edges", {}).get("p95_ms", float("nan"))),
                "p99_ms": float(abl.get("quic_only_edges", {}).get("p99_ms", float("nan"))),
            }
        },
    )

    # Ablation bar chart.
    tag = f"_{args.tag}" if args.tag else ""
    save_ablation_bars_all5(abl, outdir / f"ablation_stuck_bars_all5{tag}.png", "Stuck-rate ablation (all 5 toggles)")
    save_ablation_bars_sensitive(abl, outdir / f"ablation_stuck_bars_sensitive{tag}.png", "Stuck-rate ablation (only mechanisms that move this metric)")

    # Bandwidth breakdown.
    save_bandwidth_breakdown(results.get("meta", {}), outdir / "bandwidth_breakdown.png")

    # Healing ECDF.
    save_heal_ecdf(repo, outdir / "heal_ecdf.png")

    print(f"Wrote figures to {outdir}")


if __name__ == "__main__":
    main()


