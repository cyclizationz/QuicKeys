import argparse
import asyncio
import inspect
import random
import struct
import time
from typing import List, Tuple

from aioquic.asyncio import connect
from aioquic.quic.configuration import QuicConfiguration


def now_ms() -> int:
    return int(time.monotonic() * 1000)


def usage_for_key(key: str) -> int:
    if key == "ENTER":
        return 0x28
    if len(key) == 1:
        c = key.lower()
        if "a" <= c <= "z":
            return 0x04 + (ord(c) - ord("a"))
    return 0


def script_from_text(txt: str, repeat: int, key_delay_ms: float) -> List[Tuple[str, str, float]]:
    events: List[Tuple[str, str, float]] = []
    for _ in range(repeat):
        for ch in txt:
            key = "ENTER" if ch == "\n" else ch
            events.append(("down", key, 0.0))
            events.append(("up", key, key_delay_ms))
    return events


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", required=True)
    ap.add_argument("--port", type=int, default=4446, help="daemon QUIC edge port")
    ap.add_argument("--loss", type=float, default=0.0, help="application-level loss percent (does not model retransmissions)")
    ap.add_argument("--jitter-ms", type=float, default=0.0)
    ap.add_argument("--base-ms", type=float, default=0.0)
    ap.add_argument("--repeat", type=int, default=100)
    ap.add_argument("--key-delay-ms", type=float, default=10.0)
    ap.add_argument("--script", default="apt\n")
    args = ap.parse_args()

    import ssl
    cfg = QuicConfiguration(is_client=True, alpn_protocols=["hidra-edg"])
    cfg.verify_mode = ssl.CERT_NONE

    events = script_from_text(args.script, repeat=args.repeat, key_delay_ms=args.key_delay_ms)

    cm = connect(args.host, args.port, configuration=cfg)
    client = await cm.__aenter__()
    try:
        stream_obj = client.create_stream(is_unidirectional=False)
        if inspect.isawaitable(stream_obj):
            stream_obj = await stream_obj
        if isinstance(stream_obj, tuple) and len(stream_obj) == 2:
            _, writer = stream_obj
        else:
            writer = stream_obj

        seq = 0
        for cmd, key, delay_ms in events:
            await asyncio.sleep(delay_ms / 1000.0)
            # Timestamp BEFORE applying "network" impairment so dt_ms reflects base+jitter delay,
            # matching how the UDP edge path is evaluated in the report.
            t_ms = now_ms()
            # Apply symmetric jitter like the hybrid client does (no "positive-only" artifact).
            # Clamp to >=0 to avoid sleeping negative durations.
            d_ms = float(args.base_ms)
            if args.jitter_ms:
                d_ms += random.uniform(-float(args.jitter_ms), float(args.jitter_ms))
            if d_ms > 0:
                await asyncio.sleep(d_ms / 1000.0)
            if args.loss and random.random() < (args.loss / 100.0):
                seq += 1
                continue

            usage = usage_for_key(key)
            if not usage:
                seq += 1
                continue
            # Edge record: !IQHBB => seq(u32), t_ms(u64), usage(u16), cmd(u8), mods(u8)
            rec = struct.pack("!IQHBB", seq, t_ms, usage, 1 if cmd == "down" else 0, 0)
            writer.write(rec)
            await writer.drain()
            seq += 1
    finally:
        await cm.__aexit__(None, None, None)


if __name__ == "__main__":
    asyncio.run(main())


