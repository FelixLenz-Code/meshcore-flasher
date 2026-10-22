"""Flash jobs for ESP32 (esptool), nRF52 (serial DFU / UF2), RP2040 (UF2), STM32.

All functions are blocking; log/progress callbacks are called from the
calling thread. Raise FlashError with a user-readable text on failure.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import ports as P
from .catalog import Device, Library, check_content

ESP32_APP_ADDRESS = 0x10000
# ESP image header extended field chip_id -> esptool chip name
ESP_CHIPS = {0: "esp32", 2: "esp32s2", 5: "esp32c3", 9: "esp32s3", 12: "esp32c2", 13: "esp32c6",
             16: "esp32h2", 18: "esp32p4", 20: "esp32c61", 23: "esp32c5"}
UF2_FAMILIES = {0xADA52840: "nrf52", 0x621E937A: "nrf52", 0xE48BFF56: "rp2040", 0xE48BFF59: "rp2040"}
PCT_RE = re.compile(r"(\d{1,3}(?:\.\d+)?)\s?%")


class FlashError(Exception):
    pass


def esp_chip_of(path: Path) -> str | None:
    """Chip the image was built for, read from the ESP image header
    (classic ESP32 merged images start with 0xFF padding up to the bootloader at 0x1000)."""
    with open(path, "rb") as f:
        head = f.read(0x1010)
    for off in (0, 0x1000):
        if len(head) >= off + 16 and head[off] == 0xE9:
            return ESP_CHIPS.get(int.from_bytes(head[off + 12:off + 14], "little"))
    return None


def is_merged(path: Path) -> bool:
    if path.name.endswith("-merged.bin"):
        return True
    # a merged image has the partition table at 0x8000
    with open(path, "rb") as f:
        f.seek(0x8000)
        return f.read(2) == b"\xaa\x50"


def uf2_family(path: Path) -> str | None:
    with open(path, "rb") as f:
        block = f.read(512)
    flags = int.from_bytes(block[8:12], "little")
    if flags & 0x2000:
        return UF2_FAMILIES.get(int.from_bytes(block[28:32], "little"))
    return None


def guess_type(path: Path) -> str:
    n = path.name.lower()
    if n.endswith(".zip"):
        return "nrf52"
    if n.endswith(".uf2"):
        return uf2_family(path) or "nrf52"
    if n.endswith(".hex"):
        return "stm32"
    if n.endswith(".bin"):
        return "esp32" if esp_chip_of(path) else "stm32"
    raise FlashError(f"Unbekannter Dateityp: {path.name}")


class Flasher:
    def __init__(self, log=print, progress=lambda pct: None):
        self.log = log
        self.progress = progress
        self._proc: subprocess.Popen | None = None
        self.cancelled = False

    def cancel(self):
        self.cancelled = True
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()

    # --- entry points ---

    def flash_release(self, lib: Library, device: Device, role: str, version: str | None,
                      port: str | None, *, wipe=False, method="auto", in_bootloader=False):
        fw = device.role(role)
        if not fw:
            raise FlashError(f"{device.name} hat keine Firmware für {role}")
        ver = next((v for v in fw.versions if version in (None, v.version)), None)
        if not ver:
            raise FlashError(f"Version {version} nicht in der Bibliothek")
        files = {f.type: f for f in ver.files}

        if device.type == "esp32":
            f = files.get("flash-wipe" if wipe else "flash-update") or files.get("flash")
        elif device.type == "nrf52":
            want_uf2 = method == "uf2"
            f = files.get("download") if want_uf2 else files.get("flash")
            if f is None:  # e.g. Wio WM1110 only ships a .uf2
                f = files.get("download") or files.get("flash")
        else:
            f = files.get("flash") or files.get("download")
        if f is None:
            raise FlashError("Keine passende Firmware-Datei im Katalog")
        err = lib.verify(f)
        if err:
            raise FlashError(err)
        self.log(f"{device.name} · {fw.title} · {ver.version}")
        self.flash_file(lib.file_for(f), port, dtype=device.type, wipe=wipe, in_bootloader=in_bootloader)

    def flash_file(self, path: Path, port: str | None, *, dtype: str | None = None, wipe=False,
                   in_bootloader=False):
        path = Path(path)
        err = check_content(path)
        if err:
            raise FlashError(f"{path.name}: {err}")
        dtype = dtype or guess_type(path)
        self.log(f"Datei: {path.name}")
        name = path.name.lower()
        if dtype == "esp32":
            self.flash_esp32(path, port, wipe=wipe)
        elif name.endswith(".zip"):
            self.flash_nrf_dfu(path, port, in_bootloader=in_bootloader)
        elif name.endswith(".uf2"):
            self.flash_uf2(path, port, in_bootloader=in_bootloader)
        elif dtype == "stm32":
            self.flash_stm32(path, port)
        else:
            raise FlashError(f"Weiß nicht, wie {path.name} geflasht wird")

    # --- ESP32 ---

    def flash_esp32(self, path: Path, port: str | None, *, wipe=False):
        if not port:
            raise FlashError("Kein serieller Port gewählt")
        chip = esp_chip_of(path)
        if not chip:
            raise FlashError("Kein ESP32-Image (Header fehlt)")
        merged = is_merged(path)
        if wipe and not merged:
            raise FlashError("Komplett-Neuinstallation braucht die -merged.bin")
        addr = 0 if merged else ESP32_APP_ADDRESS
        self.log(f"Chip laut Image: {chip}, Adresse 0x{addr:x}"
                 + (", Flash wird vorher komplett gelöscht" if wipe else ""))
        args = ["--chip", chip, "--port", port, "--baud", "{baud}", "write-flash"]
        if wipe:
            args.append("--erase-all")
        args += [f"0x{addr:x}", str(path)]
        # some USB-UART bridges (cheap CH340) don't manage 460800, retry slower
        for baud in (460800, 115200):
            rc, out = self._run([sys.executable, "-m", "esptool", *[a.format(baud=baud) for a in args]])
            if rc == 0:
                self.log("Fertig. Das Gerät startet neu.")
                return
            if self.cancelled:
                raise FlashError("Abgebrochen")
            if "is " in out and ", not " in out and "This chip" in out:
                raise FlashError("Falsche Firmware: Der angeschlossene Chip passt nicht zum Image.\n"
                                 + _last_lines(out))
            if "Could not open" in out and "busy or doesn't exist" in out:
                raise FlashError(f"{port} lässt sich nicht öffnen: Gerät abgesteckt, oder ein anderes "
                                 "Programm (MeshCore Client, Serieller Monitor) hat den Port offen.")
            if "Permission denied" in out:
                raise FlashError(f"Kein Zugriff auf {port}. Benutzer muss in der Gruppe 'dialout' sein "
                                 "(sudo usermod -aG dialout $USER, danach neu anmelden).")
            if "Failed to connect" in out or "No serial data received" in out:
                hint = ("Keine Verbindung zum Bootloader. BOOT-Taste gedrückt halten, kurz RESET drücken, "
                        "BOOT loslassen und erneut flashen.")
                if baud != 115200:
                    self.log("Verbindung fehlgeschlagen, neuer Versuch mit 115200 Baud …")
                    continue
                raise FlashError(hint + "\n" + _last_lines(out))
            if baud != 115200:
                self.log("Fehler, neuer Versuch mit 115200 Baud …")
                continue
            raise FlashError("esptool ist fehlgeschlagen:\n" + _last_lines(out))

    # --- nRF52 ---

    def flash_nrf_dfu(self, path: Path, port: str | None, *, in_bootloader=False):
        if not port:
            raise FlashError("Kein serieller Port gewählt")
        if not in_bootloader:
            before = next((p for p in P.list_serial() if p.device == port), None)
            if before is None:
                raise FlashError(f"{port} ist nicht (mehr) da")
            names = {p.device for p in P.list_serial()}
            P.touch_1200(port, self.log)
            try:
                port = P.wait_for_bootloader_port(before, names, log=self.log)
            except TimeoutError as e:
                raise FlashError(str(e)) from None
            time.sleep(1.0)  # let the CDC interface settle
            self.log(f"Bootloader auf {port}")
        rc, out = self._run([sys.executable, "-m", "mcflash.nrfdfu", port, str(path)])
        if rc != 0:
            if self.cancelled:
                raise FlashError("Abgebrochen")
            raise FlashError("DFU fehlgeschlagen. Gerät per Doppelklick auf Reset in den DFU-Modus bringen "
                             "(LED pulsiert), „schon im Bootloader“ ankreuzen und erneut versuchen. "
                             "Alternativ Methode UF2 wählen.\n" + _last_lines(out))
        self.log("Fertig. Das Gerät startet neu.")

    # --- UF2 (nRF52 / RP2040) ---

    def flash_uf2(self, path: Path, port: str | None, *, in_bootloader=False):
        family = uf2_family(path)
        drive = find_uf2_drive()
        if not drive and port and not in_bootloader:
            P.touch_1200(port, self.log)
        if not drive:
            self.log("Warte auf das UF2-Laufwerk des Bootloaders …")
            drive = self._wait_uf2_drive(30)
        info = (drive / "INFO_UF2.TXT").read_text(errors="replace")
        self.log(f"Laufwerk {drive}: " + " ".join(info.split()[:12]))
        is_rp = "RP2" in info or "RPI-RP2" in drive.name or "RP2350" in info
        if family == "rp2040" and not is_rp or family == "nrf52" and is_rp:
            raise FlashError(f"UF2-Datei ist für {family}, das Laufwerk gehört aber zu einem anderen Chip")
        size = path.stat().st_size
        dest = drive / "firmware.uf2"
        self.log(f"Kopiere {size // 1024} KB …")
        written = 0
        try:
            with open(path, "rb") as src, open(dest, "wb") as out:
                for chunk in iter(lambda: src.read(64 * 1024), b""):
                    out.write(chunk)
                    written += len(chunk)
                    self.progress(100 * written / size)
                out.flush()
                os.fsync(out.fileno())
        except OSError as e:
            # the bootloader reboots as soon as the last block arrives, the
            # close/fsync after that can fail - that's success
            if written < size:
                raise FlashError(f"Kopieren abgebrochen: {e}") from None
        self.progress(100)
        self.log("Fertig. Das Gerät startet neu.")

    def _wait_uf2_drive(self, timeout) -> Path:
        deadline = time.time() + timeout
        while time.time() < deadline and not self.cancelled:
            d = find_uf2_drive(mount=True)
            if d:
                return d
            time.sleep(0.5)
        raise FlashError("Kein UF2-Laufwerk gefunden. Gerät per Doppelklick auf Reset (nRF52) bzw. mit "
                         "gedrückter BOOTSEL-Taste (RP2040) anstecken.")

    # --- STM32 (RAK3172, Wio-E5) ---

    def flash_stm32(self, path: Path, port: str | None):
        hexfile = path if path.suffix == ".hex" else None
        if shutil.which("STM32_Programmer_CLI"):
            target = ["port=SWD"] if not port else [f"port={port}"]
            cmd = ["STM32_Programmer_CLI", "-c", *target, "-w", str(path)]
            if path.suffix == ".bin":
                cmd.append("0x08000000")
            cmd += ["-v", "-rst"]
        elif shutil.which("st-flash") and not port:
            if hexfile:
                cmd = ["st-flash", "--reset", "--format", "ihex", "write", str(path)]
            else:
                cmd = ["st-flash", "--reset", "write", str(path), "0x08000000"]
        elif shutil.which("stm32flash") and port:
            self.log("UART-Bootloader: BOOT0 muss beim Reset auf HIGH liegen.")
            cmd = ["stm32flash", "-w", str(path), "-v", "-g", "0x0", port]
        else:
            raise FlashError("Für STM32 wird ein externes Programm gebraucht:\n"
                             "  ST-Link:  sudo apt install stlink-tools  (Port leer lassen)\n"
                             "  UART:     sudo apt install stm32flash   (Port wählen, BOOT0 high)\n"
                             "  oder STM32CubeProgrammer (STM32_Programmer_CLI)")
        rc, out = self._run(cmd)
        if rc != 0:
            raise FlashError("STM32-Flashen fehlgeschlagen:\n" + _last_lines(out))
        self.log("Fertig.")

    # --- helpers ---

    def run(self, cmd, cwd=None, env=None) -> tuple[int, str]:
        """Runs an external tool, streaming its output to the log."""
        return self._run(cmd, cwd=cwd, env=env)

    def _run(self, cmd, cwd=None, env=None) -> tuple[int, str]:
        self.log("$ " + " ".join(Path(c).name if i == 0 else c for i, c in enumerate(cmd)))
        env = dict(env or os.environ, PYTHONUNBUFFERED="1", NO_COLOR="1", TERM="dumb")
        env["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent)
        self._proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env, cwd=cwd)
        out = []
        buf = b""
        logged_quarter = -1
        while True:
            chunk = self._proc.stdout.read1(4096)
            if not chunk:
                break
            buf += chunk
            parts = re.split(rb"[\r\n]", buf)
            buf = parts.pop()
            for raw in parts:
                line = raw.decode(errors="replace").rstrip()
                if not line:
                    continue
                out.append(line)
                if line.startswith("PROGRESS "):
                    self.progress(float(line.split()[1]))
                    continue
                m = PCT_RE.search(line)
                if m and ("Writing" in line or "Erasing" in line):
                    pct = float(m.group(1))
                    self.progress(pct)
                    # esptool rewrites its progress line constantly; log every 25 %
                    if int(pct // 25) == logged_quarter:
                        continue
                    logged_quarter = int(pct // 25)
                    line = re.sub(r"[━╸─=>\s]{3,}", "  ", line)
                self.log(line)
        if buf.strip():
            out.append(buf.decode(errors="replace").rstrip())
            self.log(out[-1])
        rc = self._proc.wait()
        self._proc = None
        return rc, "\n".join(out)


def _last_lines(text, n=6):
    return "\n".join(text.strip().splitlines()[-n:])


def find_uf2_drive(mount=False) -> Path | None:
    """Mounted bootloader drive (has INFO_UF2.TXT). With mount=True, also mounts
    unmounted hot-plugged FAT drives through udisks (no root needed)."""
    for line in Path("/proc/mounts").read_text().splitlines():
        mp = line.split()[1].replace("\\040", " ")
        if (Path(mp) / "INFO_UF2.TXT").is_file():
            return Path(mp)
    if not mount or not shutil.which("lsblk"):
        return None
    try:
        data = json.loads(subprocess.run(
            ["lsblk", "-J", "-o", "PATH,FSTYPE,MOUNTPOINT,HOTPLUG,SIZE,LABEL", "-b"],
            capture_output=True, text=True, timeout=5).stdout)
    except (subprocess.SubprocessError, ValueError):
        return None

    def walk(nodes):
        for n in nodes:
            yield n
            yield from walk(n.get("children", []))

    for n in walk(data.get("blockdevices", [])):
        hot = n.get("hotplug") in (True, "1", 1)
        small = int(n.get("size") or 0) < 256 * 1024 * 1024  # bootloader drives are tiny
        if n.get("fstype") == "vfat" and hot and small and not n.get("mountpoint") and shutil.which("udisksctl"):
            subprocess.run(["udisksctl", "mount", "-b", n["path"], "--no-user-interaction"],
                           capture_output=True, timeout=15)
    for line in Path("/proc/mounts").read_text().splitlines():
        mp = line.split()[1].replace("\\040", " ")
        if (Path(mp) / "INFO_UF2.TXT").is_file():
            return Path(mp)
    return None
