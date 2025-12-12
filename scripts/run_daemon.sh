#!/usr/bin/env bash
set -euo pipefail
DIR="$(cd "$(dirname "$0")"/.. && pwd)"
# Match the same HID socket path used by run_qemu.sh
HID_SOCK="${HID_SOCK:-${XDG_RUNTIME_DIR:-/tmp}/hidra.kbd}"

exec "$DIR/daemon/build/daemon"   --udp-edges 0.0.0.0:4444   --hid-socket "$HID_SOCK"   --watchdog-ms 200 --deadline-ms 100   --verbose
