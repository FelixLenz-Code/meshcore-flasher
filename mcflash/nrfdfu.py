"""Serial DFU for the Adafruit nRF52 bootloader (wraps adafruit-nrfutil).

Runs as a subprocess: python -m mcflash.nrfdfu <port> <package.zip>
and prints "PROGRESS <percent>" lines that the caller parses.
"""

import json
import math
import sys
import zipfile

from nordicsemi.dfu.dfu import Dfu
from nordicsemi.dfu.dfu_transport import DfuEvent
from nordicsemi.dfu.dfu_transport_serial import DfuTransportSerial


def main():
    port, package = sys.argv[1], sys.argv[2]
    with zipfile.ZipFile(package) as z:
        manifest = json.loads(z.read("manifest.json"))["manifest"]
        size = sum(z.getinfo(m["bin_file"]).file_size for m in manifest.values() if isinstance(m, dict) and "bin_file" in m)
    frames = max(1, math.ceil(size / DfuTransportSerial.DFU_PACKET_MAX_SIZE))

    best = [0.0]

    def progress(progress=0, done=False, log_message=""):
        # events carry the frame index (plus a final 100), keep the bar monotonic
        pct = min(100.0, 100.0 * (progress + 1) / frames)
        if pct > best[0]:
            best[0] = pct
            print(f"PROGRESS {pct:.1f}", flush=True)

    transport = DfuTransportSerial(port, 115200, False, True, 0)
    transport.register_events_callback(DfuEvent.PROGRESS_EVENT, progress)
    dfu = Dfu(package, dfu_transport=transport)
    print(f"DFU über {port}, {size} Bytes", flush=True)
    dfu.dfu_send_images()
    print("PROGRESS 100", flush=True)
    print("DFU fertig.", flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001 - one readable line instead of a traceback
        print(f"FEHLER: {type(e).__name__}: {e}", flush=True)
        sys.exit(1)
