import argparse
import asyncio
import random
import socket
import struct
import time
from dataclasses import dataclass
import heapq
from typing import List, Optional, Set, Tuple

from aioquic.asyncio import connect
from aioquic.quic.configuration import QuicConfiguration


def now_ns() -> int:
    return time.monotonic_ns()


def build_snapshot(mods: int, pressed_bits: bytes, version: int = 1, seq: int = 0, t_ns: Optional[int] = None) -> bytes:
    """
    Binary snapshot format understood by daemon:
      - version: u32 big-endian
      - seq: u32 big-endian
      - t_ms: u64 big-endian (we store monotonic ms derived from t_ns)
      - mods: u32 big-endian (low 8 bits used)
      - pressed_bits: 32 bytes (256 bits)
    """
    if t_ns is None:
        t_ns = now_ns()
    t_ms = int(t_ns / 1_000_000)
    hdr = struct.pack("!IIQ", version, seq, t_ms)
    body = struct.pack("!I", mods) + pressed_bits
    return hdr + body


def usage_for_key(key: str) -> int:
    if key == "ENTER":
        return 0x28
    if len(key) == 1:
        c = key.lower()
        if "a" <= c <= "z":
            return 0x04 + (ord(c) - ord("a"))
        if c == " ":
            return 0x2C
    return 0


def bitmap_from_pressed(pressed: Set[int]) -> bytes:
    b = bytearray(32)
    for u in pressed:
        if 0 <= u < 256:
            b[u // 8] |= 1 << (u % 8)
    return bytes(b)


@dataclass
class Impairment:
    base_ms: float = 0.0
    jitter_ms: float = 0.0
    loss: float = 0.0  # 0..1

    def drop(self) -> bool:
        return self.loss > 0.0 and random.random() < self.loss

    def sample_delay_s(self) -> float:
        # Approximate netem delay base±jitter (uniform), clipped at 0
        j = random.uniform(-self.jitter_ms, self.jitter_ms) if self.jitter_ms else 0.0
        d_ms = self.base_ms + j
        if d_ms < 0:
            d_ms = 0.0
        return d_ms / 1000.0


class DelayedUdpOutbox:
    def __init__(self, sock: socket.socket, addr: Tuple[str, int], imp: Impairment):
        self._sock = sock
        self._addr = addr
        self._imp = imp
        self._pq: List[Tuple[float, bytes]] = []
        self._cv = asyncio.Condition()
        self._closed = False

    async def enqueue(self, payload: bytes, *, reliable: bool = False):
        if not reliable and self._imp.drop():
            return
        send_at = time.monotonic() + self._imp.sample_delay_s()
        async with self._cv:
            heapq.heappush(self._pq, (send_at, payload))
            self._cv.notify()

    async def run(self):
        while True:
            async with self._cv:
                while not self._pq and not self._closed:
                    await self._cv.wait()
                if self._closed and not self._pq:
                    return
                send_at, payload = heapq.heappop(self._pq)
            now = time.monotonic()
            if send_at > now:
                await asyncio.sleep(send_at - now)
            self._sock.sendto(payload, self._addr)

    async def close(self):
        async with self._cv:
            self._closed = True
            self._cv.notify_all()


class DelayedQuicOutbox:
    def __init__(self, writer, imp: Impairment):
        self._writer = writer
        self._imp = imp
        self._pq: List[Tuple[float, bytes]] = []
        self._cv = asyncio.Condition()
        self._closed = False

    async def enqueue(self, payload: bytes):
        if self._imp.drop():
            return
        send_at = time.monotonic() + self._imp.sample_delay_s()
        async with self._cv:
            heapq.heappush(self._pq, (send_at, payload))
            self._cv.notify()

    async def run(self):
        while True:
            async with self._cv:
                while not self._pq and not self._closed:
                    await self._cv.wait()
                if self._closed and not self._pq:
                    return
                send_at, payload = heapq.heappop(self._pq)
            now = time.monotonic()
            if send_at > now:
                await asyncio.sleep(send_at - now)
            self._writer.write(payload)
            await self._writer.drain()

    async def close(self):
        async with self._cv:
            self._closed = True
            self._cv.notify_all()


def build_edge_msg(cmd: str, key: str, t_ns_val: Optional[int] = None) -> bytes:
    if t_ns_val is None:
        t_ns_val = now_ns()
    return f"t={t_ns_val} {cmd} {key}".encode()


async def udp_sender(
    host: str,
    port: int,
    events: List[Tuple[float, str, str, bool]],
    imp: Impairment,
    drop_keyup_for: Optional[str],
):
    """
    events: (delay_ms, cmd, key, is_keyup_marker)
      - cmd is one of down/up/mup
      - if cmd == up and key matches drop_keyup_for, the up is dropped but the marker still sends
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    outbox = DelayedUdpOutbox(sock, (host, port), imp)
    out_task = asyncio.create_task(outbox.run())
    try:
        for delay_ms, cmd, key, is_marker in events:
            await asyncio.sleep(delay_ms / 1000.0)
            t_ns_val = now_ns()  # stamp at event time (pre-network)

            if cmd == "up" and drop_keyup_for and key == drop_keyup_for:
                continue

            payload = build_edge_msg(cmd, key, t_ns_val=t_ns_val)
            await outbox.enqueue(payload, reliable=is_marker)
    finally:
        await outbox.close()
        await out_task


async def quic_snapshot_sender(
    host: str,
    port: int,
    hz: int,
    pressed: Set[int],
    pressed_lock: asyncio.Lock,
    imp: Impairment,
    seconds: float,
):
    import ssl

    cfg = QuicConfiguration(is_client=True, alpn_protocols=["hidra-snp"])
    cfg.verify_mode = ssl.CERT_NONE

    cm = connect(host, port, configuration=cfg)
    client = await cm.__aenter__()
    try:
        # aioquic API variants
        stream_obj = client.create_stream(is_unidirectional=False)
        if asyncio.iscoroutine(stream_obj):
            stream_obj = await stream_obj
        if isinstance(stream_obj, tuple) and len(stream_obj) == 2:
            _, writer = stream_obj
        else:
            writer = stream_obj

        interval = 1.0 / hz
        seq = 0
        t0 = time.monotonic()
        outbox = DelayedQuicOutbox(writer, imp)
        out_task = asyncio.create_task(outbox.run())
        while True:
            if seconds and (time.monotonic() - t0) >= seconds:
                await outbox.close()
                await out_task
                return seq
            async with pressed_lock:
                bits = bitmap_from_pressed(set(pressed))
            snap = build_snapshot(0, bits, seq=seq)
            await outbox.enqueue(snap)
            await asyncio.sleep(interval)
            seq += 1
    finally:
        await cm.__aexit__(None, None, None)


def workload_events(text: str, repeat: int, key_delay_ms: float) -> List[Tuple[float, str, str, bool]]:
    """
    Returns events for UDP edges and marker events.
    For each character: down, (marker for up), up.
    """
    events: List[Tuple[float, str, str, bool]] = []
    for _ in range(repeat):
        for ch in text:
            key = "ENTER" if ch == "\n" else ch
            events.append((0.0, "down", key, False))
            # marker at keyup time (used to measure healing if up is dropped)
            events.append((key_delay_ms, "mup", key, True))
            events.append((0.0, "up", key, False))
            # inter-key gap
            events.append((key_delay_ms, "noop", "", False))
    # Normalize: remove noops by converting to delay-only by folding into next event
    folded: List[Tuple[float, str, str, bool]] = []
    carry = 0.0
    for d, cmd, key, ism in events:
        if cmd == "noop":
            carry += d
            continue
        folded.append((d + carry, cmd, key, ism))
        carry = 0.0
    return folded


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", required=True)
    ap.add_argument("--udp-port", type=int, default=4444)
    ap.add_argument("--quic-port", type=int, default=4445)
    ap.add_argument("--hz", type=int, default=120)
    ap.add_argument("--seconds", type=float, default=3.0)
    ap.add_argument("--workload", choices=["apt", "alphabet"], default="apt")
    ap.add_argument("--repeat", type=int, default=100)
    ap.add_argument("--key-delay-ms", type=float, default=10.0)
    ap.add_argument("--loss", type=float, default=0.0, help="loss % applied to edges and snapshots")
    ap.add_argument("--jitter-ms", type=float, default=0.0, help="uniform jitter added per packet")
    ap.add_argument("--base-ms", type=float, default=0.0, help="base delay added per packet")
    ap.add_argument("--drop-keyup", default="", help="Key name to intentionally drop keyup edge (e.g. 't' or 'ENTER')")
    ap.add_argument("--no-snapshots", action="store_true", help="Disable QUIC snapshots (UDP edges only)")
    ap.add_argument("--flush-seconds", type=float, default=0.35, help="After workload, send empty snapshots for this long to avoid end-of-run artifacts")
    args = ap.parse_args()

    if args.workload == "apt":
        text = "apt\n"
    else:
        text = "abcdefghijklmnopqrstuvwxyz\n"

    events = workload_events(text, repeat=args.repeat, key_delay_ms=args.key_delay_ms)

    pressed: Set[int] = set()
    pressed_lock = asyncio.Lock()

    # Maintain pressed set based on events (authoritative, used by snapshots)
    async def state_machine():
        for delay_ms, cmd, key, _ in events:
            await asyncio.sleep(delay_ms / 1000.0)
            u = usage_for_key(key) if key else 0
            async with pressed_lock:
                if cmd == "down" and u:
                    pressed.add(u)
                elif cmd == "up" and u:
                    pressed.discard(u)

    imp_edges = Impairment(base_ms=args.base_ms, jitter_ms=args.jitter_ms, loss=args.loss / 100.0)
    imp_quic = Impairment(base_ms=args.base_ms, jitter_ms=args.jitter_ms, loss=args.loss / 100.0)

    drop_keyup_for = args.drop_keyup if args.drop_keyup else None

    udp_task = asyncio.create_task(udp_sender(args.host, args.udp_port, events, imp_edges, drop_keyup_for))
    sm_task = asyncio.create_task(state_machine())
    quic_task = None
    if not args.no_snapshots:
        quic_task = asyncio.create_task(quic_snapshot_sender(args.host, args.quic_port, args.hz, pressed, pressed_lock, imp_quic, args.seconds))

    await asyncio.gather(udp_task, sm_task)
    sent = 0
    if quic_task is not None:
        sent = await quic_task

    # Flush: ensure the daemon sees an empty keyset and does not trip watchdog at end-of-run.
    # We do this by briefly sending snapshots with an empty pressed set.
    if not args.no_snapshots and args.flush_seconds > 0:
        async with pressed_lock:
            pressed.clear()
        await quic_snapshot_sender(args.host, args.quic_port, args.hz, pressed, pressed_lock, imp_quic, args.flush_seconds)

    print(f"done: snapshots_sent={sent} hz={args.hz} loss={args.loss}% base={args.base_ms}ms jitter={args.jitter_ms}ms no_snapshots={args.no_snapshots}")


if __name__ == "__main__":
    asyncio.run(main())


