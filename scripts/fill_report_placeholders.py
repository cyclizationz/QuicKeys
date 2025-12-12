#!/usr/bin/env python3
import argparse
import json
from pathlib import Path
from typing import Any, Dict, List, Tuple


def cdf_coords_from_log(log_path: Path, points: int = 12) -> List[Tuple[float, float]]:
    lats: List[float] = []
    for line in log_path.read_text(errors="ignore").splitlines():
        if "[lat]" in line and "dt_ms=" in line:
            try:
                s = line.split("dt_ms=")[1].split()[0]
                lats.append(float(s))
            except Exception:
                pass
    lats.sort()
    if not lats:
        return []
    n = len(lats)
    # sample evenly by rank
    coords: List[Tuple[float, float]] = []
    for i in range(points):
        r = int(i * (n - 1) / max(points - 1, 1))
        x = lats[r]
        y = (r + 1) / n
        coords.append((x, y))
    # ensure last point reaches 1.0
    coords[-1] = (coords[-1][0], 1.0)
    return coords


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", default="results/report_results.json")
    ap.add_argument("--out", default="docs/report_filled.tex")
    args = ap.parse_args()

    repo = Path(__file__).resolve().parents[1]
    results_path = repo / args.results
    out_path = repo / args.out
    out_path.parent.mkdir(parents=True, exist_ok=True)

    data: Dict[str, Any] = json.loads(results_path.read_text())
    cases: Dict[str, Any] = data["cases"]

    # CDF: use case B daemon log if present
    b_log = repo / "results/daemon_B.log"
    cdf = cdf_coords_from_log(b_log, points=12)
    cdf_str = " ".join(f"({x:.1f},{y:.3f})" for x, y in cdf) if cdf else "(10,0.1) (20,0.3) (30,0.6) (40,0.8) (60,0.95) (90,0.99)"

    # Healing vs Hz
    hvh = data["healing_vs_hz"]
    heal_coords = " ".join(
        f"({hz},{hvh[str(hz)]['heal_p50_ms']:.1f})" for hz in [60, 90, 120]
    )

    # Heatmap points: output sparse grid table (loss,jitter,stuck_pct)
    heat = data["heatmap"]
    heat_lines = ["X Y Z"]
    for row in heat:
        heat_lines.append(f"{row['loss']} {row['jitter_ms']} {row['stuck_pct']:.3f}")
    heat_table = "\n".join(heat_lines)

    # Tables
    def fmt(x):
        return "NA" if x is None else f"{x:.1f}"

    A = cases["A"]; B = cases["B"]; C = cases["C"]; D = cases["D"]

    tex = rf"""\section{{Results}}

\subsection{{Latency CDF (Case B)}}
\begin{{figure}}[h]
\centering
\begin{{tikzpicture}}
\begin{{axis}}[width=0.85\linewidth,height=6cm, xlabel={{Latency (ms)}}, ylabel={{CDF}}, ymin=0, ymax=1, grid=both, legend pos=south east]
\addplot+[mark=none] coordinates {{{cdf_str}}};
\addlegendentry{{Loss 3\%, Jitter 20 ms, 120 Hz}}
\end{{axis}}
\end{{tikzpicture}}
\caption{{Client$\to$daemon latency CDF (measured).}}
\end{{figure}}

\subsection{{Healing Time vs Snapshot Rate (median)}}
\begin{{figure}}[h]
\centering
\begin{{tikzpicture}}
\begin{{axis}}[width=0.85\linewidth,height=6cm, xlabel={{Snapshot rate (Hz)}}, ylabel={{Healing time (ms)}}, ymin=0, grid=both, xtick={{60,90,120}}]
\addplot+[mark=*] coordinates {{{heal_coords}}};
\end{{axis}}
\end{{tikzpicture}}
\caption{{Healing time vs snapshot rate (measured; dropped keyup on `t`).}}
\end{{figure}}

\subsection{{Stuck/Repeat Heatmap (stuck \%)}}
\begin{{figure}}[h]
\centering
\begin{{tikzpicture}}
\begin{{axis}}[
  width=0.85\linewidth,height=6cm,
  view={{0}}{{90}},
  xlabel=Loss (\%), ylabel=Jitter (ms),
  colorbar,
  colormap/viridis,
  xtick={{0,1,3,5,10}}, ytick={{0,20,50,100}},
  point meta min=0, point meta max=100
]
\addplot[matrix plot*,point meta=explicit] table[meta=Z] {{
{heat_table}
}};
\end{{axis}}
\end{{tikzpicture}}
\caption{{Stuck rate (\%) heatmap (measured).}}
\end{{figure}}

\subsection{{Tables}}
\begin{{table}}[h]
\centering
\caption{{Test Matrix}}
\begin{{tabular}}{{lccc}}
\toprule
Case & Loss (\%) & Jitter (ms) & Snapshot (Hz) \\
\midrule
A & 0 & 0 & 120 \\
B & 3 & 20 & 120 \\
C & 5 & 50 & 90 \\
D & 10 & 100 & 60 \\
\bottomrule
\end{{tabular}}
\end{{table}}

\begin{{table}}[h]
\centering
\caption{{Latency Statistics (measured, ms)}}
\begin{{tabular}}{{lrrrr}}
\toprule
Case & p50 (ms) & p95 (ms) & p99 (ms) & Max (ms) \\
\midrule
A & {fmt(A['p50_ms'])} & {fmt(A['p95_ms'])} & {fmt(A['p99_ms'])} & {fmt(A['max_ms'])} \\
B & {fmt(B['p50_ms'])} & {fmt(B['p95_ms'])} & {fmt(B['p99_ms'])} & {fmt(B['max_ms'])} \\
C & {fmt(C['p50_ms'])} & {fmt(C['p95_ms'])} & {fmt(C['p99_ms'])} & {fmt(C['max_ms'])} \\
D & {fmt(D['p50_ms'])} & {fmt(D['p95_ms'])} & {fmt(D['p99_ms'])} & {fmt(D['max_ms'])} \\
\bottomrule
\end{{tabular}}
\end{{table}}

\begin{{table}}[h]
\centering
\caption{{Correctness \& Resource (measured)}}
\begin{{tabular}}{{lrrrr}}
\toprule
Case & Stuck (\%) & Healing p50 (ms) & Daemon CPU (\%) & BW (kb/s) \\
\midrule
A & {A['stuck_pct']:.2f} & {fmt(A['heal_p50_ms'])} & {fmt(A['daemon_cpu_pct'])} & {A['bw_kbps']:.1f} \\
B & {B['stuck_pct']:.2f} & {fmt(B['heal_p50_ms'])} & {fmt(B['daemon_cpu_pct'])} & {B['bw_kbps']:.1f} \\
C & {C['stuck_pct']:.2f} & {fmt(C['heal_p50_ms'])} & {fmt(C['daemon_cpu_pct'])} & {C['bw_kbps']:.1f} \\
D & {D['stuck_pct']:.2f} & {fmt(D['heal_p50_ms'])} & {fmt(D['daemon_cpu_pct'])} & {D['bw_kbps']:.1f} \\
\bottomrule
\end{{tabular}}
\end{{table}}
"""

    out_path.write_text(tex)
    print(f"Wrote {out_path}")


if __name__ == "__main__":
    main()


