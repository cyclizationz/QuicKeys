# QuicKeys: Hypervisor-Based HID for Robust, Low-Latency Remote Input (QEMU + msquic)

(Former working title: HiDra)

# ==========================================
## Abstract
QuicKeys combines low-latency UDP edge events with reliable QUIC snapshots, reconciled in a C++ host daemon that emits standard USB-HID reports into a QEMU guest. This heals missed releases within one snapshot interval while preserving input feel and avoiding guest hooks—ideal for terminal/text validation, with gaming as future work.

## Motivation

Recent remote desktop and cloud-gaming systems still suffer from “stuck” or repeated keys under loss and jitter (e.g.,
typing apt yields appppppt). The root cause is simple but pernicious: a delayed or dropped keyup leaves the guest OS
believing a key is held and its auto-repeat fires until a release is finally seen. Gaming pipelines highlight the
latency–reliability trade-off starkly, but the failure is just as harmful for text-centric tasks (shells, editors,
terminals), where exactness matters more than raw frame rate.

Fully reliable streams (e.g., TCP) avoid loss but introduce head-of-line blocking that hurts input feel; fully
unreliable streams (e.g., UDP) are snappy but fragile under loss. User-mode injection inside the guest can also conflict
with integrity/anti-cheat mechanisms. QuicKeys targets the sweet spot: preserve input feel while making the stream
self-correcting and keeping the guest side clean by presenting a standard HID device via the hypervisor.

## Problem Statement

Can we deliver sub-50 ms end-to-end input latency and zero stuck/repeat keys at ≤5% loss for terminal/editor workloads
by (i) streaming edge events over UDP for immediacy, (ii) sending authoritative snapshots over QUIC for fast correction,
and (iii) emitting standard USB-HID into a QEMU guest—without any guest hooks that could conflict with anti-cheat or OS
integrity?

## Approach

### Transport Hybrid.

QuicKeys splits input into two paths: (1) UDP edge events (keydown/keyup) sent immediately with per-key sequence IDs and
redundant keyup; and (2) QUIC snapshots (TLS 1.3) at 60–120 Hz carrying the complete pressed-key bitmap plus modifiers.
The host daemon applies edges instantly to retain responsiveness, then uses the next snapshot to reconcile any drift
caused by lost/delayed packets. This makes the stream effectively “stateless on the wire”: the latest snapshot heals
earlier losses within one snapshot interval.

### Host-side State & Safety.

A small C++ state machine (Boost.Asio + protobuf + msquic) maintains an authoritative set of pressed keys, applies
deadline filtering (drop events that arrive >100 ms late), and runs a force-release watchdog (~200 ms) to unstick keys
that haven’t been reaffirmed by snapshots—critical under deep jitter. All events carry client-side monotonic timestamps
for measurement and debugging.

### Guest Integration.

The daemon converts the authoritative key set into 8-byte USB-HID reports and feeds them into a QEMU USB-HID socket
backend. The guest sees a normal keyboard device; no user-mode injection or kernel hooks inside the guest. (A
virtio-input backend can be added later for Linux-only optimization; for this validation we keep USB-HID for maximum
compatibility.)

## Technical Contributions (expected)

A snapshot-healed input stream that eliminates repeated/stuck keys without adding meaningful latency.

A hypervisor-presented HID path that preserves native OS semantics and remains compatible with integrity/anti-cheat
expectations.

A measurable test harness (netem loss/jitter, evtest capture, CSV/plots) reproducible on commodity Linux.

## Experimental Methodology

We evaluate with Linux guests and scripted text workloads (terminal commands, editor bursts). Using tc netem, we sweep
loss {0, 1, 3, 5, 10%} and jitter {0, 20, 50, 100 ms} at snapshot rates {60, 90, 120 Hz}. Metrics:

Latency client→guest (p50/p95) from stamped events,

Stuck-key rate (% unreleased within 300 ms),

Healing time after intentionally dropped keyup,

CPU overhead (daemon + QEMU input thread),

Bandwidth for edges/snapshots.

Success criteria: avg latency < 50 ms, 0% stuck keys at ≤ 5% loss, healing ≤ snapshot interval, CPU < 5% of one core,
bandwidth < 50 kb/s for typing.

## Expected Milestones

1. Transport + state core (UDP edges, QUIC snapshot receiver via msquic, protobuf schema, HID report builder, CSV
   logging).

2. QEMU USB-HID socket backend + smoke tests; baseline metrics with no impairment.

3. Hardening (redundant keyup, seq de-dup, watchdog/deadline tuning); full QUIC TLS; metrics dashboard.

4. Full loss/jitter matrix; analysis & charts; 10-page report with recommendations and next-steps (virtio-input,
   attestation).

## Risks & Mitigations

1. QEMU integration drift (different versions): begin with USB-HID socket backend, keep changes localized and guarded;
   pin a specific QEMU tag.

2. Clock comparability: rely on monotonic deltas and in-band timestamps; optional Chrony/NTP if needed.

3. QUIC complexity: use msquic library with examples; start with plaintext QUIC, then add TLS.

## Ethics & Compliance

QuicKeys intentionally avoids guest-side hooks and does not attempt to bypass anti-cheat. For gaming contexts,
certification/whitelisting and provenance/attestation are the appropriate next steps; this prototype focuses on
correctness and latency for text/terminal use.