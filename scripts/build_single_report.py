#!/usr/bin/env python3
import argparse
import json
from pathlib import Path
from typing import Any, Dict


def fmt1(x):
    if x is None:
        return "NA"
    return f"{float(x):.1f}"


def fmt2(x):
    if x is None:
        return "NA"
    return f"{float(x):.2f}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", default="results/report_results.json")
    ap.add_argument("--out", default="docs/report_single.tex")
    ap.add_argument("--bib", default="docs/refs.bib")
    args = ap.parse_args()

    repo = Path(__file__).resolve().parents[1]
    data: Dict[str, Any] = json.loads((repo / args.results).read_text())
    cases = data["cases"]
    hvh = data["healing_vs_hz"]

    out_path = repo / args.out
    out_path.parent.mkdir(parents=True, exist_ok=True)

    A, B, C, D = cases["A"], cases["B"], cases["C"], cases["D"]

    tex = rf"""
\documentclass[11pt]{{article}}

% --- Template-compatible packages (matches your Report_Template preamble style) ---
\usepackage[utf8]{{inputenc}}
\usepackage{{amsmath, amssymb, amsfonts}}
\usepackage[numbers]{{natbib}}
\usepackage{{graphicx}}
\usepackage{{float}}
\usepackage{{caption}}
\usepackage{{subcaption}}
\usepackage{{tabularx}}
\usepackage{{tabu}}
\usepackage{{booktabs}}
\usepackage[nottoc,numbib]{{tocbibind}}
\usepackage[margin = 2.5cm]{{geometry}}
\usepackage{{microtype}}
\usepackage{{titlesec}}
\usepackage{{titletoc}}
\usepackage{{appendix}}
\usepackage{{fancyhdr}}
\usepackage[shortlabels]{{enumitem}}
\usepackage{{hyperref}}
\usepackage[noabbrev, capitalise]{{cleveref}}
\usepackage{{xcolor}}

\title{{QuickKeys: Snapshot-Healed Hypervisor HID for Robust Low-Latency Remote Input}}
\author{{Tiehang Zhang (200598590)}}
\date{{}}

\begin{{document}}
\maketitle
\tableofcontents
\newpage

\begin{{abstract}}
Remote keyboard input over lossy networks must balance responsiveness with correctness. In practice, delayed or dropped key-release events can cause the receiver to interpret a key as continuously held, triggering operating-system auto-repeat (e.g., typing \texttt{{apt}} becomes \texttt{{appppppt}}). Conventional reliable byte streams such as TCP can mitigate loss but introduce head-of-line blocking, which can inflate tail latency for interactive input. Conversely, UDP preserves immediacy but provides no intrinsic healing when events are lost. This report evaluates a hybrid transport design that transmits immediate key edges over UDP and transmits periodic authoritative key-state snapshots over QUIC. A host-side C++ daemon reconciles both streams, applies bounded staleness filtering (deadline) and a watchdog fail-safe, and emits standard USB-HID keyboard reports into a QEMU guest, avoiding guest-side injection hooks. Using scripted terminal workloads and a controlled loss/jitter sweep, we quantify client-to-daemon latency, snapshot-bounded healing after missed releases, and the resulting stuck-key rate. Overall, the results support the hypothesis that periodic snapshots bound recovery time while preserving low perceived latency on the edge path.
\end{{abstract}}

\section{{Motivation \& Problem}}
Remote control and cloud-gaming stacks frequently experience correctness failures when key-release events are delayed or lost. Because most guest operating systems implement key auto-repeat when a key remains logically pressed, a single missing \texttt{{keyup}} can lead to unbounded repetition until recovery occurs. A common mitigation is to rely on reliable transport; however, reliable byte streams such as TCP \citep{{rfc9293}} can introduce head-of-line blocking, where loss of an earlier segment delays delivery of later input events. UDP-based delivery preserves responsiveness but provides no built-in recovery from loss. In addition, guest-side injection mechanisms (user-mode hooks or kernel drivers) can conflict with integrity or policy constraints, motivating a hypervisor-presented device model.
Our goal is to deliver a remote keyboard path with sub-\SI{{50}}{{ms}} median client-to-daemon latency while achieving zero stuck or repeated keys at loss rates up to \SI{{5}}{{\percent}}. We pursue this goal by combining low-latency UDP edge delivery with authoritative QUIC snapshots \citep{{rfc9000,rfc9001}}, and by presenting a standard USB-HID keyboard device to the guest \citep{{usb_hid}} via QEMU \citep{{qemu}}, thereby avoiding guest-side hooks.

\section{{Background and Related Work}}
QUIC \citep{{rfc9000}} provides secure, congestion-controlled transport over UDP with multiplexed streams and TLS 1.3 security \citep{{rfc9001}}. It can reduce head-of-line blocking relative to TCP by isolating loss recovery to the affected stream rather than a single global byte stream. Linux network emulation via \texttt{{tc netem}} \citep{{netem}} is commonly used to reproduce controlled loss and delay conditions in experimental evaluations. In virtualization, QEMU \citep{{qemu}} provides device emulation, and the USB-HID class specification \citep{{usb_hid}} defines a widely compatible keyboard report format that guests consume without special drivers. Finally, msquic \citep{{msquic}} provides a production QUIC implementation used here to terminate snapshot streams on the host.

\section{{System Under Test}}
The system comprises four components: (i) a Python client that sends UDP edge events and QUIC snapshots; (ii) a C++ host daemon that maintains an authoritative key set, drops excessively stale events (deadline filter), and force-releases unreaffirmed keys (watchdog); (iii) QEMU configured with a patched \texttt{{usb-kbd}} backend that ingests 8-byte HID reports via a \texttt{{-chardev socket}}; and (iv) a Linux guest whose correctness can be validated via \texttt{{evtest}}.
\begin{{figure}}[H]
  \centering
  \includegraphics[width=\linewidth]{{figures/architecture.png}}
  \caption{{Architecture overview of the hybrid UDP+QUIC input pipeline.}}
  \label{{fig:arch}}
\end{{figure}}

\section{{Experimental Setup}}
\subsection{{Hardware \& OS}}
Experiments are conducted on a Linux host with KVM enabled and a Linux guest. CPU frequency scaling is set to \texttt{{performance}} to reduce timing variability.
\subsection{{Network Impairments}}
We emulate loss and jitter using Linux traffic control netem \citep{{netem}}. A representative configuration is:
\begin{{verbatim}}
sudo tc qdisc add dev lo root netem delay 60ms 20ms loss 3%
sudo tc qdisc del dev lo root   # cleanup
\end{{verbatim}}
\subsection{{Workloads}}
We evaluate three text-centric workloads. The terminal burst repeats the sequence ``\texttt{{apt ENTER}}'' multiple times to exercise rapid press/release patterns. The alphabet burst types \texttt{{abcdefghijklmnopqrstuvwxyz}} followed by a newline to stress a larger key set. The healing test intentionally drops a single \texttt{{keyup}} and measures the time until a snapshot forces the corresponding release.
\subsection{{Independent Variables}}
We sweep loss rates in \(\{{0,1,3,5,10\}}\%\), jitter in \(\{{0,20,50,100\}}\)\,ms with a base delay of \SI{{60}}{{ms}}, and snapshot rates in \(\{{60,90,120\}}\)\,Hz.
\subsection{{Measurement Points}}
Latency is measured from the client timestamp embedded in each UDP edge to the daemon receipt time. Healing time is measured from an injected keyup marker to the first snapshot-induced release observed by the daemon. CPU and bandwidth are estimated using OS counters and transmitted message sizes.

\section{{Evaluation Criteria}}
The system passes if (i) median latency is below \SI{{50}}{{ms}} and p95 latency is below \SI{{120}}{{ms}}; (ii) the stuck/repeat rate is \SI{{0}}{{\percent}} at loss rates up to \SI{{5}}{{\percent}}; (iii) healing is bounded by at most one snapshot interval; (iv) daemon overhead remains below \SI{{5}}{{\percent}} of one CPU core; and (v) bandwidth remains below \SI{{50}}{{kb/s}} at \SI{{120}}{{Hz}}.

\section{{Results}}
\subsection{{Latency CDF (Case B)}}
\begin{{figure}}[H]
  \centering
  \includegraphics[width=0.9\linewidth]{{figures/latency_cdf_caseB.png}}
  \caption{{Client$\to$daemon latency CDF under loss=\SI{{3}}{{\percent}}, jitter=\SI{{20}}{{ms}}, snapshot=\SI{{120}}{{Hz}}. Latency is computed from the client-side monotonic timestamp in each UDP edge to the daemon receipt time.}}
  \label{{fig:cdf}}
\end{{figure}}
Under moderate loss and jitter (Case~B), latency remains concentrated around the configured base delay, with tail growth primarily driven by added delay variation rather than transport head-of-line blocking. \textbf{{Conclusion:}} the UDP edge path maintains interactive latency while snapshots provide correctness recovery.

\subsection{{Healing Time vs Snapshot Rate}}
\begin{{figure}}[H]
  \centering
  \includegraphics[width=0.9\linewidth]{{figures/healing_vs_hz.png}}
  \caption{{Healing time vs snapshot rate when a \texttt{{keyup}} is intentionally dropped. Healing is measured from a client-side keyup marker to the first snapshot-induced release observed by the daemon; smaller is better.}}
  \label{{fig:heal}}
\end{{figure}}
Increasing the snapshot rate reduces the time-to-correct after a missed release; the observed median decreases from {fmt1(hvh["60"]["heal_p50_ms"])}\,ms at \SI{{60}}{{Hz}} to {fmt1(hvh["120"]["heal_p50_ms"])}\,ms at \SI{{120}}{{Hz}}. \textbf{{Conclusion:}} snapshot cadence provides a predictable control knob for the recovery bound.

\subsection{{Stuck/Repeat Heatmap}}
\begin{{figure}}[H]
  \centering
  \includegraphics[width=0.9\linewidth]{{figures/stuck_heatmap.png}}
  \caption{{Measured stuck rate (\%) over the loss/jitter sweep at \SI{{120}}{{Hz}}. ``Stuck'' denotes releases that exceed \SI{{300}}{{ms}} to heal or require watchdog release.}}
  \label{{fig:heat}}
\end{{figure}}
The stuck rate increases with stronger impairments, reflecting that both edge delivery and confirmations are more likely to arrive outside the configured deadline/watchdog window. \textbf{{Conclusion:}} snapshots suppress most stuck behavior under moderate impairment, while the watchdog remains a necessary backstop under worst-case conditions.

\subsection{{Tables}}
\begin{{table}}[H]
  \centering
  \caption{{Test matrix used for the main latency and correctness evaluation (Cases A--D).}}
  \label{{tab:matrix}}
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

\begin{{table}}[H]
  \centering
  \caption{{Client$\to$daemon latency statistics (ms) derived from timestamped UDP edge receipts at the daemon.}}
  \label{{tab:lat}}
  \begin{{tabular}}{{lrrrr}}
    \toprule
    Case & p50 (ms) & p95 (ms) & p99 (ms) & Max (ms) \\
    \midrule
    A & {fmt1(A["p50_ms"])} & {fmt1(A["p95_ms"])} & {fmt1(A["p99_ms"])} & {fmt1(A["max_ms"])} \\
    B & {fmt1(B["p50_ms"])} & {fmt1(B["p95_ms"])} & {fmt1(B["p99_ms"])} & {fmt1(B["max_ms"])} \\
    C & {fmt1(C["p50_ms"])} & {fmt1(C["p95_ms"])} & {fmt1(C["p99_ms"])} & {fmt1(C["max_ms"])} \\
    D & {fmt1(D["p50_ms"])} & {fmt1(D["p95_ms"])} & {fmt1(D["p99_ms"])} & {fmt1(D["max_ms"])} \\
    \bottomrule
  \end{{tabular}}
\end{{table}}

\begin{{table}}[H]
  \centering
  \caption{{Correctness and resource summary. ``Stuck'' reports the fraction of key-release markers not resolved within \SI{{300}}{{ms}} (or requiring watchdog release). Healing time is the median snapshot-induced correction time when releases are missed.}}
  \label{{tab:cr}}
  \begin{{tabular}}{{lrrrr}}
    \toprule
    Case & Stuck (\%) & Healing p50 (ms) & Daemon CPU (\%) & BW (kb/s) \\
    \midrule
    A & {fmt2(A["stuck_pct"])} & {fmt1(A["heal_p50_ms"])} & {fmt1(A["daemon_cpu_pct"])} & {fmt1(A["bw_kbps"])} \\
    B & {fmt2(B["stuck_pct"])} & {fmt1(B["heal_p50_ms"])} & {fmt1(B["daemon_cpu_pct"])} & {fmt1(B["bw_kbps"])} \\
    C & {fmt2(C["stuck_pct"])} & {fmt1(C["heal_p50_ms"])} & {fmt1(C["daemon_cpu_pct"])} & {fmt1(C["bw_kbps"])} \\
    D & {fmt2(D["stuck_pct"])} & {fmt1(D["heal_p50_ms"])} & {fmt1(D["daemon_cpu_pct"])} & {fmt1(D["bw_kbps"])} \\
    \bottomrule
  \end{{tabular}}
\end{{table}}
The tables reinforce the figure-based trends: tail latency increases under stronger impairment, healing remains snapshot-bounded, and the watchdog activates primarily in the most severe case. \textbf{{Conclusion:}} the hybrid design trades modest bandwidth for correctness under loss without converting the edge path into a reliable stream.

\section{{Discussion \& Takeaways}}
Authoritative snapshots provide a bounded recovery mechanism for missed releases while allowing edges to remain latency-sensitive on UDP. The watchdog acts as a secondary safety net when snapshots are delayed beyond the configured bound. Extending the design to gaming will require more detailed timing semantics and stronger integrity integration.

\section{{Reproducibility Checklist}}
We pin the device model to QEMU 8.2.0 and use a USB-HID report path to avoid guest modifications. The daemon is invoked with \texttt{{--watchdog-ms 200 --deadline-ms 100 --verbose}}. Network impairment settings are recorded per run using \texttt{{tc netem}} \citep{{netem}}. Scripts under \texttt{{scripts/}} produce logs and aggregate metrics.

\bibliographystyle{{plainnat}}
\bibliography{{refs}}

\end{{document}}
"""
    out_path.write_text(tex.strip() + "\n")
    print(f"Wrote {out_path}")


if __name__ == "__main__":
    main()


