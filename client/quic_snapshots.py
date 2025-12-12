import argparse, asyncio, time, struct
import inspect
from aioquic.asyncio import connect
from aioquic.quic.configuration import QuicConfiguration

def build_snapshot(mods: int, pressed_bits: bytes, version=1, seq=0, t_ms=None) -> bytes:
    # Minimal toy format for now (replace with protobuf in real build)
    if t_ms is None:
        t_ms = int(time.monotonic() * 1000)
    hdr = struct.pack("!IIQ", version, seq, t_ms)
    body = struct.pack("!I", mods) + pressed_bits
    return hdr + body

async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", required=True)
    ap.add_argument("--port", type=int, default=4445)
    ap.add_argument("--hz", type=int, default=120)
    ap.add_argument("--timeout", type=float, default=5.0, help="Connection timeout in seconds")
    ap.add_argument("--seconds", type=float, default=0.0, help="Send for N seconds then exit (0 = forever)")
    args = ap.parse_args()

    import ssl
    cfg = QuicConfiguration(is_client=True, alpn_protocols=["hidra-snp"])
    # Disable certificate verification for development (self-signed certificate)
    cfg.verify_mode = ssl.CERT_NONE
    try:
        # Apply timeout only to the QUIC handshake / connection establishment, not the send loop.
        cm = connect(args.host, args.port, configuration=cfg)
        client = await asyncio.wait_for(cm.__aenter__(), timeout=args.timeout)
        try:
            # aioquic API differences across versions:
            # - create_stream(...) might be sync or async
            # - return value might be a StreamWriter or (StreamReader, StreamWriter)
            print(f"Connected to {args.host}:{args.port} (QUIC). Opening stream...")

            try:
                stream_obj = client.create_stream(is_unidirectional=False)
            except TypeError:
                # Older/newer aioquic variants
                try:
                    stream_obj = client.create_stream()
                except TypeError:
                    stream_id = client._quic.get_next_available_stream_id(is_unidirectional=False)
                    stream_obj = client.create_stream(stream_id)

            if inspect.isawaitable(stream_obj):
                stream_obj = await stream_obj

            if isinstance(stream_obj, tuple) and len(stream_obj) == 2:
                _, writer = stream_obj
            else:
                writer = stream_obj

            print("Stream opened. Sending snapshots...")

            interval = 1.0 / args.hz
            seq = 0
            t0 = time.monotonic()
            while True:
                if args.seconds and (time.monotonic() - t0) >= args.seconds:
                    print(f"Done (sent {seq} snapshots).")
                    return 0
                # Example: mods=0, pressed_bits all zero (no keys pressed)
                snap = build_snapshot(0, b"\x00" * 32, seq=seq)
                writer.write(snap)
                await writer.drain()
                await asyncio.sleep(interval)
                seq += 1
        finally:
            await cm.__aexit__(None, None, None)
    except asyncio.TimeoutError:
        print(f"Error: Connection to {args.host}:{args.port} timed out after {args.timeout}s")
        print("Make sure the daemon is running with msquic support enabled.")
        print("Run: scripts/run_daemon.sh")
        return 1
    except Exception as e:
        print(f"Error: Failed to connect to {args.host}:{args.port}: {e}")
        return 1

if __name__ == "__main__":
    exit(asyncio.run(main()) or 0)
