#!/usr/bin/env python3
"""Real 300MB transfer measurement over send_hermes (Phase 6 scenario).

Builds a ~300MB file (incompressible random data — worst case, no
compression benefit), sends it with RealSendHermesTransport, fetches it
back, verifies BLAKE3, and prints measured throughput. This is the honest
real-scale counterpart of tests/test_phase6_hardening.py::test_12 (which
runs the 30MB scaled fixture over FakeTransport in pytest).

Measured on the build host (~2 MB/s network ceiling): expect ~150-300s.
Run in the FOREGROUND with a generous timeout:

  env -u PYTHONNOUSERSITE HOME=/home/maxcurrent PATH=/usr/bin:/bin \
    HERMES_IROH_RELAY=off /usr/bin/python3.10 scripts/measure_300mb.py
"""
import hashlib
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from transport import RealSendHermesTransport

SIZE = 300 * 1024 * 1024


def main():
    import tempfile
    t0 = time.time()
    with tempfile.TemporaryDirectory(prefix="agentpool-300mb-") as tmp:
        tmp = Path(tmp)
        payload = tmp / "payload-300mb.bin"
        print(f"generating {SIZE} bytes of random data...", flush=True)
        with open(payload, "wb") as fh:
            remaining = SIZE
            while remaining > 0:
                chunk = os.urandom(min(1024 * 1024, remaining))
                fh.write(chunk)
                remaining -= len(chunk)
        sha = hashlib.blake3(payload.read_bytes()).hexdigest() if hasattr(hashlib, "blake3") else None
        from crypto import blake3_hash
        src_hash = blake3_hash(payload.read_bytes())
        print(f"generated in {time.time()-t0:.1f}s, blake3={src_hash[:16]}...", flush=True)

        transport = RealSendHermesTransport(state_dir=tmp / "state")
        t_send = time.time()
        sent = transport.send(payload)
        print(f"ticket={sent['ticket'][:24]}... send_setup={time.time()-t_send:.1f}s",
              flush=True)
        dest = tmp / "fetch"
        dest.mkdir()
        t_fetch = time.time()
        out = transport.fetch(sent["ticket"], dest)
        fetch_secs = time.time() - t_fetch
        data = out.read_bytes()
        assert len(data) == SIZE, f"size mismatch: {len(data)}"
        assert blake3_hash(data) == src_hash, "BLAKE3 mismatch"
        transport.stop(sent["transfer_id"])
        mbps = SIZE / fetch_secs / 1e6
        print(f"FETCH_OK size={SIZE} fetch_seconds={fetch_secs:.1f} "
              f"throughput={mbps:.2f} MB/s blake3_verified=true")
        print(f"TOTAL_SECONDS={time.time()-t0:.1f} THROUGHPUT_MBPS={mbps:.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
