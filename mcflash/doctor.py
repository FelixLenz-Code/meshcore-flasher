"""System checks for common Linux pitfalls when flashing over USB serial."""

import grp
import os
import shutil
import subprocess
import sys
from pathlib import Path

RULES_SRC = Path(__file__).resolve().parent.parent / "99-meshcore-flasher.rules"
RULES_DST = Path("/etc/udev/rules.d/99-meshcore-flasher.rules")
RULES_PKG = Path("/usr/lib/udev/rules.d/99-meshcore-flasher.rules")  # shipped by the .deb


def checks() -> list[tuple[bool, str]]:
    out = []
    try:
        dialout = grp.getgrnam("dialout")
        in_group = dialout.gr_gid in os.getgroups()
        listed = os.environ.get("USER", "") in dialout.gr_mem
        if in_group:
            out.append((True, "Benutzer ist in der Gruppe dialout"))
        elif listed:
            out.append((False, "Gruppe dialout ist eingetragen, aber noch nicht aktiv: einmal ab- und anmelden"))
        else:
            out.append((False, "Kein Zugriff auf serielle Ports: sudo usermod -aG dialout $USER, dann neu anmelden"))
    except KeyError:
        pass

    if _active("ModemManager"):
        ok = udev_rule_ok()
        out.append((ok, "ModemManager läuft" + (", udev-Regel vorhanden" if ok else
                    ": kann nRF52-DFU stören. Abhilfe: „Jetzt beheben“ bzw. "
                    "meshcore-flasher doctor --fix")))
    if shutil.which("brltty"):
        out.append((False, "brltty ist installiert und kapert gern CH340-Adapter (1a86:7523). "
                    "Wenn kein /dev/ttyUSB* erscheint: sudo apt remove brltty"))
    out.append((bool(shutil.which("udisksctl")), "udisksctl für UF2-Laufwerke"
                + ("" if shutil.which("udisksctl") else " fehlt (sudo apt install udisks2)")))
    for mod, what in (("esptool", "esptool (ESP32)"), ("nordicsemi", "adafruit-nrfutil (nRF52)")):
        try:
            __import__(mod)
            out.append((True, what))
        except ImportError:
            out.append((False, f"{what} fehlt: ./install.sh ausführen"))
    stm = [t for t in ("STM32_Programmer_CLI", "st-flash", "stm32flash") if shutil.which(t)]
    out.append((bool(stm), "STM32: " + (", ".join(stm) if stm else
                "kein Programm (nur für RAK3172/Wio-E5 nötig: sudo apt install stlink-tools stm32flash)")))
    return out


def udev_rule_ok() -> bool:
    for dst in (RULES_DST, RULES_PKG):
        try:
            if dst.read_bytes() == RULES_SRC.read_bytes():
                return True
        except OSError:
            pass
    return False


def udev_fix_needed() -> bool:
    """ModemManager runs and our rule is missing or outdated."""
    return _active("ModemManager") and not udev_rule_ok()


def install_udev_rule() -> tuple[bool, str]:
    """Installs the rule as root: pkexec (password dialog) without a terminal, else sudo."""
    script = ('install -m 644 "$1" "$2" && udevadm control --reload-rules '
              '&& udevadm trigger --subsystem-match=tty --action=add')
    elevate = ["sudo"] if sys.stdin.isatty() or not shutil.which("pkexec") else ["pkexec"]
    try:
        r = subprocess.run([*elevate, "sh", "-c", script, "sh", str(RULES_SRC), str(RULES_DST)],
                           capture_output=elevate == ["pkexec"], text=True, timeout=300)
    except (OSError, subprocess.SubprocessError) as e:
        return False, f"Konnte {elevate[0]} nicht starten: {e}"
    if r.returncode == 0 and udev_rule_ok():
        return True, "udev-Regel installiert: ModemManager lässt MeshCore-Geräte jetzt in Ruhe."
    if r.returncode in (126, 127) and elevate == ["pkexec"]:
        return False, "Abgebrochen (kein Admin-Passwort eingegeben)."
    return False, "Installation fehlgeschlagen" + (f": {r.stderr.strip()}" if r.stderr else "")


def _active(unit) -> bool:
    try:
        return subprocess.run(["systemctl", "is-active", "--quiet", unit], timeout=5).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False
