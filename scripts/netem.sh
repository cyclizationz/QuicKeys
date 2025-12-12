#!/usr/bin/env bash
# Usage: scripts/netem.sh add|del <dev> [base_ms jitter_ms loss_pct]
set -euo pipefail
cmd=${1:-help}
dev=${2:-eth0}
if [[ "$cmd" == "add" ]]; then
  base_ms=${3:-60}; jitter_ms=${4:-20}; loss_pct=${5:-3}
  sudo tc qdisc add dev "$dev" root netem delay ${base_ms}ms ${jitter_ms}ms loss ${loss_pct}%
  echo "Added netem on $dev: delay ${base_ms}ms ${jitter_ms}ms loss ${loss_pct}%"
elif [[ "$cmd" == "del" ]]; then
  sudo tc qdisc del dev "$dev" root || true
  echo "Deleted netem on $dev"
else
  echo "Usage: $0 add|del <dev> [base_ms jitter_ms loss_pct]"; exit 1
fi
