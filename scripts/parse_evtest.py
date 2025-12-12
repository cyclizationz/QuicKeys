#!/usr/bin/env python3
import argparse
import csv
import re
from collections import Counter
from pathlib import Path
from typing import Dict, Tuple


RE_EV_KEY = re.compile(r"EV_KEY\s+(KEY_[A-Z0-9_]+)\s+(?P<val>[01])")


def key_to_letter(key: str) -> str:
    # KEY_A..KEY_Z -> a..z
    if key.startswith("KEY_") and len(key) == 5:
        ch = key[-1]
        if "A" <= ch <= "Z":
            return ch.lower()
    if key == "KEY_ENTER":
        return "ENTER"
    return ""


def parse_evtest(path: Path) -> Dict[str, Counter]:
    """
    Produces per-key counts: presses, releases.
    A simple "repeat" heuristic can be added later if needed.
    """
    c_press = Counter()
    c_release = Counter()
    for ln in path.read_text(errors="ignore").splitlines():
        m = RE_EV_KEY.search(ln)
        if not m:
            continue
        k = m.group(1)
        val = int(m.group("val"))
        name = key_to_letter(k) or k
        if val == 1:
            c_press[name] += 1
        else:
            c_release[name] += 1
    return {"press": c_press, "release": c_release}


def confusion_table(stats: Dict[str, Counter], keys: Tuple[str, ...]) -> Dict[str, Dict[str, int]]:
    out: Dict[str, Dict[str, int]] = {}
    for k in keys:
        p = int(stats["press"][k])
        r = int(stats["release"][k])
        out[k] = {
            "presses": p,
            "releases": r,
            "missed_releases": max(0, p - r),
            "extra_releases": max(0, r - p),
        }
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", required=True, help="path to evtest log")
    ap.add_argument("--out", dest="out", default="", help="optional CSV output path")
    args = ap.parse_args()

    p = Path(args.inp)
    stats = parse_evtest(p)
    keys = tuple([chr(ord("a") + i) for i in range(26)])
    tab = confusion_table(stats, keys)

    # Print a compact summary
    total_press = sum(tab[k]["presses"] for k in keys)
    total_miss = sum(tab[k]["missed_releases"] for k in keys)
    print(f"total_presses={total_press} total_missed_releases={total_miss}")

    if args.out:
        outp = Path(args.out)
        outp.parent.mkdir(parents=True, exist_ok=True)
        with outp.open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["key", "presses", "releases", "missed_releases", "extra_releases"])
            w.writeheader()
            for k in keys:
                row = {"key": k, **tab[k]}
                w.writerow(row)
        print(f"wrote_csv={outp}")


if __name__ == "__main__":
    main()


