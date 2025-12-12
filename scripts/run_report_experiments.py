#!/usr/bin/env python3
import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time
import shutil
import math
import stat
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple


RE_LAT = re.compile(r"\[lat\]\s+dt_ms=(?P<dt>[0-9.]+)")
RE_HEAL = re.compile(r"\[heal\]\s+key=(?P<key>\d+)\s+heal_ms=(?P<ms>[0-9.]+)")
RE_WD = re.compile(r"\[watchdog\]\s+key=(?P<key>\d+)\s+age_ms=(?P<ms>[0-9.]+)")
RE_STUCK = re.compile(r"\[stuck\]\s+key=(?P<key>\d+)\s+age_ms=(?P<ms>[0-9.]+)")


def _parse_float_prefix(s: str) -> Optional[float]:
    """
    Robust float parsing for log fields that may get interleaved (e.g., '50.188161.1809').
    We take the first float-looking prefix and ignore any trailing garbage.
    """
    m = re.match(r"^\d+(?:\.\d+)?", s)
    if not m:
        return None
    try:
        return float(m.group(0))
    except Exception:
        return None


def percentile(xs: List[float], p: float) -> Optional[float]:
    if not xs:
        return None
    xs2 = sorted(xs)
    if p <= 0:
        return xs2[0]
    if p >= 100:
        return xs2[-1]
    k = (len(xs2) - 1) * (p / 100.0)
    f = int(k)
    c = min(f + 1, len(xs2) - 1)
    if f == c:
        return xs2[f]
    d0 = xs2[f] * (c - k)
    d1 = xs2[c] * (k - f)
    return d0 + d1


def _safe_float(x: Optional[float]) -> Optional[float]:
    if x is None:
        return None
    if isinstance(x, float) and (math.isnan(x) or math.isinf(x)):
        return None
    return float(x)


def kill_pids(pids: List[int]):
    for pid in sorted(set(pids)):
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except PermissionError:
            pass


def pids_listening_udp_ports(ports: List[int]) -> List[int]:
    # Parse: ss -H -u -l -n -p
    # Example users:(("daemon",pid=45927,fd=8))
    try:
        out = subprocess.check_output(["ss", "-H", "-u", "-l", "-n", "-p"], text=True, stderr=subprocess.DEVNULL)
    except Exception:
        return []
    pids: List[int] = []
    for line in out.splitlines():
        for port in ports:
            if f":{port} " not in line and not line.rstrip().endswith(f":{port}"):
                continue
            for m in re.finditer(r"pid=(\d+)", line):
                pids.append(int(m.group(1)))
    return pids


@dataclass
class Case:
    name: str
    loss_pct: float
    jitter_ms: float
    snapshot_hz: int
    base_ms: float = 60.0


@dataclass
class CaseResult:
    name: str
    n_lat: int
    p50_ms: Optional[float]
    p95_ms: Optional[float]
    p99_ms: Optional[float]
    max_ms: Optional[float]
    heals: int
    heal_p50_ms: Optional[float]
    heal_p95_ms: Optional[float]
    watchdog_fires: int
    stuck_pct: float
    bw_kbps: float
    daemon_cpu_pct: Optional[float]
    daemon_cpu_mean: Optional[float] = None
    daemon_cpu_std: Optional[float] = None
    qemu_cpu_mean: Optional[float] = None
    qemu_cpu_std: Optional[float] = None


def hid_sock_default() -> str:
    # Keep consistent with scripts/run_qemu.sh which uses ${XDG_RUNTIME_DIR:-/tmp}/hidra.kbd.
    # In some non-interactive shells XDG_RUNTIME_DIR may be unset; /run/user/<uid> is a good fallback.
    if os.environ.get("HID_SOCK"):
        return os.environ["HID_SOCK"]
    xdg = os.environ.get("XDG_RUNTIME_DIR")
    if xdg:
        return os.path.join(xdg, "hidra.kbd")
    run_user = f"/run/user/{os.getuid()}"
    if os.path.isdir(run_user):
        return os.path.join(run_user, "hidra.kbd")
    return "/tmp/hidra.kbd"


def is_unix_socket(path: str) -> bool:
    try:
        st = os.stat(path)
        return stat.S_ISSOCK(st.st_mode)
    except Exception:
        return False


def parse_log(path: Path) -> Tuple[List[float], List[float], int]:
    lats: List[float] = []
    heals: List[float] = []
    watchdog = 0
    for line in path.read_text(errors="ignore").splitlines():
        m = RE_LAT.search(line)
        if m:
            v = _parse_float_prefix(m.group("dt"))
            if v is not None:
                lats.append(v)
        m = RE_HEAL.search(line)
        if m:
            v = _parse_float_prefix(m.group("ms"))
            if v is not None:
                heals.append(v)
        m = RE_WD.search(line)
        if m:
            watchdog += 1
    return lats, heals, watchdog


def parse_watchdog_keys(path: Path) -> set[int]:
    keys: set[int] = set()
    for line in path.read_text(errors="ignore").splitlines():
        m = RE_WD.search(line)
        if m:
            try:
                keys.add(int(m.group("key")))
            except Exception:
                pass
    return keys


def parse_stuck_keys(path: Path) -> set[int]:
    keys: set[int] = set()
    for line in path.read_text(errors="ignore").splitlines():
        m = RE_STUCK.search(line)
        if m:
            try:
                keys.add(int(m.group("key")))
            except Exception:
                pass
    return keys


def estimate_duration_s(workload: str, repeat: int, key_delay_ms: float) -> float:
    if workload == "apt":
        text_len = 4
    else:
        text_len = 27
    # per char: down + (marker after key_delay) + up + inter-key key_delay
    per_char_ms = key_delay_ms + key_delay_ms + 1.0
    total_ms = text_len * repeat * per_char_ms
    return total_ms / 1000.0


def proc_cpu_pct(pid: int, duration_s: float) -> float:
    """
    Very rough CPU % of one core using /proc/<pid>/stat deltas over duration_s.
    """
    hz = os.sysconf(os.sysconf_names["SC_CLK_TCK"])
    try:
        with open(f"/proc/{pid}/stat", "r") as f:
            a = f.read().split()
        ut0, st0 = int(a[13]), int(a[14])
        time.sleep(duration_s)
        with open(f"/proc/{pid}/stat", "r") as f:
            b = f.read().split()
        ut1, st1 = int(b[13]), int(b[14])
    except Exception:
        return float("nan")
    cpu_s = ((ut1 - ut0) + (st1 - st0)) / hz
    return max(0.0, min(100.0, 100.0 * cpu_s / max(duration_s, 1e-6)))


def pid_for_unix_listener(sock_path: str) -> Optional[int]:
    """
    Best-effort parse of `ss -xlp` to find the PID listening on a given unix socket path.
    """
    try:
        out = subprocess.check_output(["ss", "-H", "-x", "-l", "-p"], text=True, stderr=subprocess.DEVNULL)
    except Exception:
        return None
    for line in out.splitlines():
        if sock_path not in line:
            continue
        m = re.search(r"pid=(\d+)", line)
        if m:
            try:
                return int(m.group(1))
            except Exception:
                return None
    return None


def pidstat_sample(pid: int, duration_s: float, out_path: Path) -> Tuple[Optional[float], Optional[float]]:
    """
    Sample CPU% using pidstat once per second and return (mean, stddev).
    Saves raw pidstat output to out_path for debugging.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    n = max(2, int(math.ceil(duration_s)))
    cmd = ["pidstat", "-p", str(pid), "1", str(n)]
    try:
        p = subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False)
        out_path.write_text(p.stdout)
    except Exception:
        return None, None

    # Parse %CPU column (robust to locale/extra spacing).
    lines = out_path.read_text(errors="ignore").splitlines()
    header_idx = None
    cols = []
    for i, ln in enumerate(lines):
        if "%CPU" in ln and "PID" in ln:
            header_idx = i
            cols = ln.split()
            break
    if header_idx is None:
        return None, None
    try:
        cpu_col = cols.index("%CPU")
        pid_col = cols.index("PID")
    except ValueError:
        return None, None

    samples: List[float] = []
    for ln in lines[header_idx + 1 :]:
        if not ln.strip() or ln.startswith("Average:"):
            continue
        parts = ln.split()
        if len(parts) <= max(cpu_col, pid_col):
            continue
        try:
            if int(parts[pid_col]) != pid:
                continue
        except Exception:
            continue
        v = _parse_float_prefix(parts[cpu_col])
        if v is not None:
            samples.append(v)
    if not samples:
        return None, None
    mean = float(sum(samples) / len(samples))
    var = float(sum((x - mean) ** 2 for x in samples) / max(1, (len(samples) - 1)))
    return mean, math.sqrt(var)

def _run(cmd: List[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, **kwargs)


def sudo_ok() -> bool:
    # Non-interactive sudo check; caller can run `sudo -v` once in shell to prime credentials.
    try:
        p = _run(["sudo", "-n", "true"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return p.returncode == 0
    except Exception:
        return False


def tc_clear(dev: str = "lo") -> None:
    # Best-effort cleanup; ignore errors when qdisc isn't present.
    if not sudo_ok():
        return
    _run(["sudo", "-n", "tc", "qdisc", "del", "dev", dev, "root"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def tc_apply(loss_pct: float, base_ms: float, jitter_ms: float, dev: str = "lo") -> bool:
    if not sudo_ok():
        return False
    # Use `replace` to avoid "handle of zero" warnings and to make repeated runs idempotent.
    if jitter_ms and jitter_ms > 0:
        cmd = ["sudo", "-n", "tc", "qdisc", "replace", "dev", dev, "root", "netem",
               "delay", f"{base_ms}ms", f"{jitter_ms}ms",
               "loss", f"{loss_pct}%"]
    else:
        cmd = ["sudo", "-n", "tc", "qdisc", "replace", "dev", dev, "root", "netem",
               "delay", f"{base_ms}ms",
               "loss", f"{loss_pct}%"]
    p = _run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return p.returncode == 0


def run_case(
    repo: Path,
    env_name: str,
    case: Case,
    workload: str,
    repeat: int,
    key_delay_ms: float,
    drop_keyup: str = "",
    out_dir: Path = Path("results_next"),
    watchdog_ms: int = 200,
    deadline_ms: int = 100,
    stuck_ms: int = 300,
    enable_snapshots: bool = True,
    use_tc: bool = False,
    client_mode: str = "hybrid",  # hybrid|quic_only
) -> CaseResult:
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / f"daemon_{case.name}.log"
    hid_sock = hid_sock_default()

    print(f"\n== case={case.name} workload={workload} repeat={repeat} ==")
    print(f"  impairment: base_ms={case.base_ms} jitter_ms={case.jitter_ms} loss_pct={case.loss_pct} snapshot_hz={case.snapshot_hz}")
    print(f"  daemon: watchdog_ms={watchdog_ms} deadline_ms={deadline_ms} stuck_ms={stuck_ms} enable_snapshots={enable_snapshots} client_mode={client_mode}")
    print(f"  HID_SOCK: {hid_sock} exists={os.path.exists(hid_sock)} is_socket={is_unix_socket(hid_sock)}")
    if not os.path.exists(hid_sock) or not is_unix_socket(hid_sock):
        print("  hint: QEMU must listen on HID_SOCK. Run: `sh scripts/run_qemu.sh` (it creates the socket),")
        print("        or set HID_SOCK to match QEMU, e.g.: `export HID_SOCK=${XDG_RUNTIME_DIR:-/tmp}/hidra.kbd`")

    # Ensure no previous daemon is holding ports (common during iterative runs)
    subprocess.run(["pkill", "-9", "-x", "daemon"], cwd=str(repo), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    kill_pids(pids_listening_udp_ports([4444, 4445]))

    daemon = None
    lf = None
    # Start daemon with retry if ports are still in use.
    for attempt in range(1, 4):
        daemon_cmd = [
            str(repo / "daemon/build/daemon"),
            "--udp-edges",
            "0.0.0.0:4444",
            "--hid-socket",
            hid_sock,
            "--watchdog-ms",
            str(watchdog_ms),
            "--deadline-ms",
            str(deadline_ms),
            "--stuck-ms",
            str(stuck_ms),
            "--verbose",
        ]
        print(f"  start daemon (attempt {attempt}/3): {' '.join(daemon_cmd)}")
        lf = log_path.open("w")
        daemon = subprocess.Popen(
            daemon_cmd,
            cwd=str(repo),
            stdout=lf,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )

        start = time.time()
        ok = False
        while time.time() - start <= 6:
            time.sleep(0.1)
            txt = ""
            try:
                txt = log_path.read_text(errors="ignore")
            except Exception:
                pass
            if "Address already in use" in txt:
                # kill and retry
                subprocess.run(["pkill", "-9", "-x", "daemon"], cwd=str(repo), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                kill_pids(pids_listening_udp_ports([4444, 4445]))
                try:
                    daemon.kill()
                except Exception:
                    pass
                break
            if "msquic snapshot listener" in txt:
                ok = True
            if ok:
                break
            if daemon.poll() is not None:
                break

        if ok and daemon.poll() is None:
            # Note: HID socket connects lazily on first send_report(), which only happens after the client starts.
            break

        # cleanup between attempts
        if daemon and daemon.poll() is None:
            daemon.kill()
        if lf:
            try:
                lf.close()
            except Exception:
                pass
        if attempt == 3:
            raise RuntimeError(
                f"Daemon failed to start for case={case.name} (ports busy or startup crash). "
                f"See {log_path}."
            )

    # Estimate runtime and run CPU sampler in background
    runtime_s = estimate_duration_s(workload, repeat, key_delay_ms) + 1.5
    cpu_pct: Optional[float] = None
    daemon_cpu_mean = None
    daemon_cpu_std = None
    qemu_cpu_mean = None
    qemu_cpu_std = None
    qemu_pid = pid_for_unix_listener(hid_sock)
    if daemon.poll() is None:
        cpu_pct = _safe_float(proc_cpu_pct(daemon.pid, min(2.0, runtime_s)))

    # Optionally apply kernel netem (recommended for the QUIC-only baseline).
    # If sudo isn't primed, fall back to app-level impairment in the client.
    tc_enabled = False
    if use_tc:
        tc_enabled = tc_apply(case.loss_pct, case.base_ms, case.jitter_ms, dev="lo")
        if not tc_enabled:
            print("[warn] --use-tc requested but sudo -n failed (needs interactive auth).")
            print("       hint: run `sudo -v` once in your terminal, then rerun with --use-tc.")
        else:
            print("  tc netem enabled on lo (kernel-level impairment)")

    # Run client workload + snapshots / baselines
    seconds = runtime_s
    micromamba = (
        os.environ.get("MICROMAMBA")
        or shutil.which("micromamba")
        or str(Path.home() / ".local/bin/micromamba")
    )
    if client_mode == "quic_only":
        # QUIC-only baseline uses a separate ALPN/port on the daemon (:4446).
        client_cmd = [
            micromamba, "run", "-n", env_name, "python", "client/baselines_quic_only.py",
            "--host", "127.0.0.1",
            "--port", "4446",
            "--repeat", str(repeat),
            "--key-delay-ms", str(key_delay_ms),
            "--script", "apt\n" if workload == "apt" else "abcdefghijklmnopqrstuvwxyz\n",
        ]
        if not tc_enabled:
            # Without tc netem, we still apply base/jitter to keep the x-axis comparable to UDP edges,
            # but we keep loss=0 to avoid turning a reliability baseline into an app-level drop test.
            # Use --use-tc to study HOL under loss with kernel-level impairment.
            client_cmd += ["--base-ms", str(case.base_ms), "--jitter-ms", str(case.jitter_ms), "--loss", "0"]
    else:
        base_ms = 0.0 if tc_enabled else case.base_ms
        jitter_ms = 0.0 if tc_enabled else case.jitter_ms
        loss_pct = 0.0 if tc_enabled else case.loss_pct
        client_cmd = [
            micromamba, "run", "-n", env_name, "python", "client/hybrid_run.py",
            "--host", "127.0.0.1",
            "--workload", workload,
            "--repeat", str(repeat),
            "--hz", str(case.snapshot_hz),
            "--seconds", str(seconds),
            "--base-ms", str(base_ms),
            "--jitter-ms", str(jitter_ms),
            "--loss", str(loss_pct),
            "--key-delay-ms", str(key_delay_ms),
        ]
        if not enable_snapshots:
            client_cmd += ["--no-snapshots"]
        if drop_keyup:
            client_cmd += ["--drop-keyup", drop_keyup]

    try:
        print(f"  run client: {' '.join(client_cmd)}")
        subprocess.run(client_cmd, cwd=str(repo), check=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    finally:
        if tc_enabled:
            tc_clear("lo")

    # pidstat sampling after the run (uses the daemon PID; for QEMU we sample the listener PID).
    # We sample for up to 60s or runtime_s, whichever is smaller.
    try:
        sample_s = min(60.0, runtime_s)
        if daemon and daemon.pid and daemon.poll() is None:
            daemon_cpu_mean, daemon_cpu_std = pidstat_sample(daemon.pid, sample_s, out_dir / f"pidstat_daemon_{case.name}.log")
        if qemu_pid:
            qemu_cpu_mean, qemu_cpu_std = pidstat_sample(qemu_pid, sample_s, out_dir / f"pidstat_qemu_{case.name}.log")
    except Exception:
        pass

    # Post-check HID connection (it is lazy and should have happened once events were emitted).
    try:
        txt = log_path.read_text(errors="ignore")
        if "Connected to HID socket" not in txt:
            print(f"[warn] daemon never connected to HID socket during case={case.name}")
            print("       hint: verify QEMU is running with `sh scripts/run_qemu.sh`")
            print(f"       hint: verify socket exists+LISTEN: `ls -l {hid_sock}` and `ss -xl | grep hidra.kbd`")
    except Exception:
        pass

    # Allow any delayed packets to flush
    time.sleep(0.5)

    # Stop daemon
    if daemon.poll() is None:
        daemon.send_signal(signal.SIGINT)
        try:
            daemon.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            daemon.kill()
    try:
        if lf:
            lf.close()
    except Exception:
        pass

    # Parse metrics
    lats, heals, watchdog = parse_log(log_path)
    watchdog_keys = parse_watchdog_keys(log_path)
    stuck_keys = parse_stuck_keys(log_path)

    # Total "keyup markers" approx equals number of characters typed (each char has a marker)
    total_markers = (4 if workload == "apt" else 27) * repeat
    # "Stuck" definition for report: keys unreleased beyond a tolerance window.
    # We approximate with:
    # - healing events that took >300ms, plus
    # - keys that required watchdog release (dedup by key)
    # This yields a bounded rate in [0,100].
    stuck_events = sum(1 for h in heals if h > 300.0) + len(watchdog_keys) + len(stuck_keys)
    stuck_pct = min(100.0, 100.0 * stuck_events / max(total_markers, 1))

    # Bandwidth estimate: edges are short ascii (~20-30B), snapshots fixed 52B payload + QUIC overhead ignored.
    # This is a lower bound; good enough for the placeholder table.
    edges_per_char = 3  # down + marker + up
    edge_bytes = total_markers * edges_per_char * 28
    snaps = int(seconds * case.snapshot_hz)
    quic_bytes = snaps * 52
    bw_kbps = (edge_bytes + quic_bytes) * 8 / max(seconds, 1e-6) / 1000.0

    return CaseResult(
        name=case.name,
        n_lat=len(lats),
        p50_ms=_safe_float(percentile(lats, 50)),
        p95_ms=_safe_float(percentile(lats, 95)),
        p99_ms=_safe_float(percentile(lats, 99)),
        max_ms=_safe_float(max(lats) if lats else None),
        heals=len(heals),
        heal_p50_ms=_safe_float(percentile(heals, 50)),
        heal_p95_ms=_safe_float(percentile(heals, 95)),
        watchdog_fires=watchdog,
        stuck_pct=stuck_pct,
        bw_kbps=bw_kbps,
        daemon_cpu_pct=cpu_pct,
        daemon_cpu_mean=_safe_float(daemon_cpu_mean),
        daemon_cpu_std=_safe_float(daemon_cpu_std),
        qemu_cpu_mean=_safe_float(qemu_cpu_mean),
        qemu_cpu_std=_safe_float(qemu_cpu_std),
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--env", default="quickeys_client", help="micromamba env name")
    ap.add_argument("--out", default="results_next/report_results.json")
    ap.add_argument("--repeat", type=int, default=100)
    ap.add_argument("--key-delay-ms", type=float, default=10.0)
    ap.add_argument("--use-tc", action="store_true", help="Use tc netem on lo via sudo -n (requires sudo already authenticated).")
    ap.add_argument("--matrix-base-ms", type=float, default=60.0, help="Base delay (ms) for A–D matrix/healing/heatmap runs (default: 60)")
    ap.add_argument(
        "--matrix-deadline-ms",
        type=int,
        default=0,
        help="Deadline filter (ms) for the A–D latency matrix only. Use 0 to disable truncation (default: 0).",
    )
    ap.add_argument(
        "--run",
        default="all",
        help="Comma-separated sections to run: all,matrix,ablations,healing,heatmap. "
             "Example: --run ablations (fast; updates only results['ablations_caseB']).",
    )
    ap.add_argument("--abl-loss", type=float, default=3.0, help="Loss percent used for ablations (default: 3)")
    ap.add_argument("--abl-jitter-ms", type=float, default=20.0, help="Jitter ms used for ablations (default: 20)")
    ap.add_argument("--abl-base-ms", type=float, default=60.0, help="Base delay ms used for ablations (default: 60)")
    ap.add_argument("--abl-hz", type=int, default=120, help="Snapshot Hz used for ablations (default: 120)")
    args = ap.parse_args()

    repo = Path(__file__).resolve().parents[1]
    out_path = Path(repo / args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # A–D matrix from the report
    cases = [
        Case("A", loss_pct=0, jitter_ms=0, snapshot_hz=120, base_ms=args.matrix_base_ms),
        Case("B", loss_pct=3, jitter_ms=20, snapshot_hz=120, base_ms=args.matrix_base_ms),
        Case("C", loss_pct=5, jitter_ms=50, snapshot_hz=90, base_ms=args.matrix_base_ms),
        Case("D", loss_pct=10, jitter_ms=100, snapshot_hz=60, base_ms=args.matrix_base_ms),
    ]

    # Load existing results to support partial reruns.
    results: Dict[str, Any]
    if out_path.exists():
        try:
            results = json.loads(out_path.read_text())
        except Exception:
            results = {}
    else:
        results = {}
    results.setdefault("cases", {})
    results["meta"] = {"repeat": args.repeat, "key_delay_ms": args.key_delay_ms}

    run_set = {s.strip() for s in args.run.split(",") if s.strip()}
    if "all" in run_set:
        run_set = {"matrix", "ablations", "healing", "heatmap"}

    if "matrix" in run_set:
        for c in cases:
            r = run_case(
                repo,
                args.env,
                c,
                workload="apt",
                repeat=args.repeat,
                key_delay_ms=args.key_delay_ms,
                # For A–D latency characterization we disable the deadline filter by default;
                # otherwise high-jitter cases get truncated and percentiles become conditional on acceptance.
                deadline_ms=int(args.matrix_deadline_ms),
                use_tc=args.use_tc,
            )
            results["cases"][c.name] = asdict(r)

    if "ablations" in run_set:
        # Ablations / baselines (run on Case B conditions to isolate mechanism differences)
        ablations = {}
        base_case = Case("abl_base", loss_pct=args.abl_loss, jitter_ms=args.abl_jitter_ms, snapshot_hz=args.abl_hz, base_ms=args.abl_base_ms)
        ablations["full"] = asdict(run_case(repo, args.env, Case("abl_full", base_case.loss_pct, base_case.jitter_ms, base_case.snapshot_hz, base_case.base_ms), "apt", args.repeat, args.key_delay_ms, use_tc=args.use_tc))
        ablations["no_watchdog"] = asdict(run_case(repo, args.env, Case("abl_no_watchdog", base_case.loss_pct, base_case.jitter_ms, base_case.snapshot_hz, base_case.base_ms), "apt", args.repeat, args.key_delay_ms, watchdog_ms=0, use_tc=args.use_tc))
        ablations["no_deadline"] = asdict(run_case(repo, args.env, Case("abl_no_deadline", base_case.loss_pct, base_case.jitter_ms, base_case.snapshot_hz, base_case.base_ms), "apt", args.repeat, args.key_delay_ms, deadline_ms=0, use_tc=args.use_tc))
        # Controls/baselines should trigger the known failure mode by forcing a missing keyup.
        ablations["udp_only_no_snapshots"] = asdict(
            run_case(repo, args.env, Case("abl_udp_only", base_case.loss_pct, base_case.jitter_ms, base_case.snapshot_hz, base_case.base_ms), "apt", args.repeat, args.key_delay_ms,
                     enable_snapshots=False, drop_keyup="t", use_tc=args.use_tc)
        )
        ablations["udp_only_no_snapshots_no_watchdog"] = asdict(
            run_case(repo, args.env, Case("abl_udp_only_no_watchdog", base_case.loss_pct, base_case.jitter_ms, base_case.snapshot_hz, base_case.base_ms), "apt", args.repeat, args.key_delay_ms,
                     enable_snapshots=False, watchdog_ms=0, drop_keyup="t", use_tc=args.use_tc)
        )
        ablations["quic_only_edges"] = asdict(
            run_case(repo, args.env, Case("abl_quic_only", base_case.loss_pct, base_case.jitter_ms, base_case.snapshot_hz, base_case.base_ms), "apt", args.repeat, args.key_delay_ms,
                     # QUIC-only edges can experience sender-side pacing / buffering which inflates dt_ms.
                     # Disable deadline filtering for this baseline so we can measure the full latency CDF.
                     deadline_ms=0,
                     client_mode="quic_only", use_tc=args.use_tc)
        )
        results["ablations_caseB"] = ablations

    if "healing" in run_set:
        # Healing-vs-Hz (drop one keyup)
        heal_rates = [60, 90, 120]
        heal_results = {}
        for hz in heal_rates:
            c = Case(f"heal_{hz}", loss_pct=3, jitter_ms=20, snapshot_hz=hz, base_ms=args.matrix_base_ms)
            r = run_case(repo, args.env, c, workload="apt", repeat=50, key_delay_ms=args.key_delay_ms, drop_keyup="t", use_tc=args.use_tc)
            heal_results[str(hz)] = {"heal_p50_ms": r.heal_p50_ms, "heal_p95_ms": r.heal_p95_ms}
        results["healing_vs_hz"] = heal_results

    if "heatmap" in run_set:
        # Stuck heatmap (loss x jitter) at 120 Hz
        heat = []
        for loss in [0, 1, 3, 5, 10]:
            for jit in [0, 20, 50, 100]:
                name = f"hm_l{loss}_j{jit}"
                c = Case(name, loss_pct=loss, jitter_ms=jit, snapshot_hz=120, base_ms=args.matrix_base_ms)
                r = run_case(repo, args.env, c, workload="alphabet", repeat=20, key_delay_ms=args.key_delay_ms, use_tc=args.use_tc)
                heat.append({"loss": loss, "jitter_ms": jit, "stuck_pct": r.stuck_pct})
        results["heatmap"] = heat

    # Ensure we never silently emit NaN/Infinity (wastes time and breaks post-processing).
    out_path.write_text(json.dumps(results, indent=2, allow_nan=False))
    print(f"Wrote {out_path}")


if __name__ == "__main__":
    main()


