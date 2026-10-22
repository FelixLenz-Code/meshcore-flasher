"""Command line interface (meshcore-flasher <command>)."""

import argparse
import sys
from pathlib import Path

from . import __version__, addons, doctor, ports, state
from .paths import library_root
from .catalog import ROLE_TITLES, TYPE_TITLES, Library
from .flasher import Flasher, FlashError

ROLE_ALIASES = {"ble": "companionBle", "usb": "companionUsb", "companion": "companionBle",
                "repeater": "repeater", "room": "roomServer", "roomserver": "roomServer",
                "kiss": "kissRadio"}


def main(argv=None):
    ap = argparse.ArgumentParser(prog="meshcore-flasher", description="MeshCore-Firmware offline flashen")
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("sync", help="Neueste Firmware aus dem Internet holen")
    s.add_argument("--device", "-d", action="append", help="nur Geräte, deren Name das enthält (mehrfach)")
    s.add_argument("--versions", type=int, default=1, help="Anzahl neuester Versionen (Standard 1)")
    s.add_argument("--catalog-only", action="store_true", help="nur Geräteliste, keine Dateien")

    s = sub.add_parser("list", help="Geräte in der Bibliothek")
    s.add_argument("filter", nargs="?", default="")

    sub.add_parser("ports", help="Angeschlossene Geräte")
    s = sub.add_parser("doctor", help="System prüfen")
    s.add_argument("--fix", action="store_true", help="udev-Regel gegen ModemManager installieren (sudo)")

    s = sub.add_parser("flash", help="Firmware aus der Bibliothek flashen")
    s.add_argument("device", help="Gerätename oder Teil davon (siehe list)")
    s.add_argument("role", help="ble | usb | repeater | room | kiss")
    s.add_argument("--port", "-p", action="append", help="serieller Port (mehrfach = mehrere Geräte nacheinander)")
    s.add_argument("--version", "-V")
    s.add_argument("--wipe", action="store_true", help="ESP32: Flash komplett löschen (merged.bin)")
    s.add_argument("--uf2", action="store_true", help="nRF52: über UF2-Laufwerk statt Serial-DFU")
    s.add_argument("--in-bootloader", action="store_true", help="Gerät ist schon im DFU/Bootloader-Modus")

    s = sub.add_parser("file", help="Beliebige Datei flashen (.bin/.zip/.uf2/.hex)")
    s.add_argument("path")
    s.add_argument("--port", "-p")
    s.add_argument("--wipe", action="store_true")
    s.add_argument("--in-bootloader", action="store_true")

    s = sub.add_parser("erase", help="nRF52: externen/internen Flash mit Lösch-Firmware leeren")
    s.add_argument("device")
    s.add_argument("--port", "-p")
    s.add_argument("--in-bootloader", action="store_true")

    s = sub.add_parser("addons", help="Eigene Patches (addons/) bauen")
    s.add_argument("action", choices=["list", "build", "setup"],
                   help="setup: PlatformIO einmalig installieren (Internet)")
    s.add_argument("name", nargs="?", help="Addon (Standard: alle veralteten)")
    s.add_argument("--version", "-V", help="Release-Version, Standard: neueste im Katalog")

    a = ap.parse_args(argv)
    lib = Library(library_root())

    if a.cmd == "sync":
        try:
            lib.sync(devices=a.device, versions=a.versions, download=not a.catalog_only,
                     progress=lambda p: None)
        except OSError as e:
            sys.exit(f"Kein Internet / Server nicht erreichbar: {e}")
        for n, have, want in addons.outdated(lib):
            print(f"Addon {n}: gebaut für {have or '–'}, neu: {want}  →  meshcore-flasher addons build")
        return

    if a.cmd == "addons":
        if a.action == "setup":
            try:
                print(f"PlatformIO bereit: {addons.setup_pio(Flasher(log=print).run, print)}")
            except FlashError as e:
                sys.exit(f"FEHLER: {e}")
            return
        if a.action == "list":
            todo = {n: (have, want) for n, have, want in addons.outdated(lib)}
            for n, ad in addons.available().items():
                have = addons.built_version(lib, ad)
                state_txt = f"gebaut für {have}" if have else "noch nicht gebaut"
                if n in todo:
                    state_txt += f", neu bauen für {todo[n][1]}"
                print(f"{n}: {ad['title']} ({ad['device']})  –  {state_txt}")
            return
        names = [a.name] if a.name else [n for n, _h, _w in addons.outdated(lib)]
        if not names:
            print("Alle Addons sind aktuell.")
        fl = Flasher(log=print)
        try:
            for n in names:
                addons.build(lib, n, fl.run, print, version=a.version)
        except FlashError as e:
            sys.exit(f"FEHLER: {e}")
        return

    if a.cmd == "doctor":
        if a.fix:
            if not doctor.udev_fix_needed():
                print("✓ Nichts zu beheben (ModemManager läuft nicht oder die udev-Regel ist aktuell)")
            else:
                ok, msg = doctor.install_udev_rule()
                print(("✓ " if ok else "✗ ") + msg)
        for ok, text in doctor.checks():
            print(("✓ " if ok else "✗ ") + text)
        present, listed = lib.status()
        print(f"{'✓' if present == listed and listed else '✗'} Bibliothek: {present}/{listed} Dateien "
              f"(Stand {lib.synced or 'nie synchronisiert'})")
        return

    if a.cmd == "ports":
        for p in ports.list_serial():
            fam = TYPE_TITLES.get(p.family, "?")
            mem = state.recall(p.key)
            extra = f"  zuletzt: {mem['device']} / {mem['role']}" if mem else ""
            print(f"{p.label}  ({fam}){extra}")
        return

    if a.cmd == "list":
        if not lib.devices:
            sys.exit("Bibliothek leer: zuerst 'meshcore-flasher sync' (mit Internet)")
        for d in lib.devices:
            if a.filter.lower() not in d.name.lower():
                continue
            roles = ", ".join(f"{fw.title} {fw.versions[0].version}"
                              + ("" if all(lib.have(f) for f in fw.versions[0].files) else " (fehlt)")
                              for fw in d.firmware)
            print(f"{d.name}  [{TYPE_TITLES.get(d.type, d.type)}]\n    {roles}")
        return

    fl = Flasher(log=print, progress=_bar)
    try:
        if a.cmd == "flash":
            dev = _device(lib, a.device)
            role = _role(dev, a.role)
            port_list = a.port or [None]
            for i, port in enumerate(port_list):
                if len(port_list) > 1:
                    print(f"\n=== Gerät {i + 1}/{len(port_list)}: {port} ===")
                # key before flashing: the bootloader may show up under another name
                key = _port_key(port)
                fl.flash_release(lib, dev, role, a.version, port, wipe=a.wipe,
                                 method="uf2" if a.uf2 else "auto", in_bootloader=a.in_bootloader)
                if key:
                    state.remember(key, dev.id, role)
        elif a.cmd == "file":
            fl.flash_file(Path(a.path), a.port, wipe=a.wipe, in_bootloader=a.in_bootloader)
        elif a.cmd == "erase":
            dev = _device(lib, a.device)
            if not dev.erase:
                sys.exit(f"Für {dev.name} gibt es keine Lösch-Firmware")
            fl.flash_file(lib.path(dev.erase), a.port, dtype="nrf52", in_bootloader=a.in_bootloader)
            print("Lösch-Firmware läuft. Danach das Gerät per Doppelklick auf Reset wieder in den "
                  "DFU-Modus bringen und MeshCore mit --in-bootloader flashen.")
    except FlashError as e:
        print(flush=True)
        sys.exit(f"FEHLER: {e}")
    except KeyboardInterrupt:
        fl.cancel()
        sys.exit("Abgebrochen")


def _device(lib, key):
    d = lib.device(key)
    if not d:
        hits = [x.name for x in lib.devices if key.lower() in x.name.lower()]
        sys.exit(f"Gerät '{key}' nicht eindeutig gefunden" + (": " + ", ".join(hits) if hits else ""))
    return d


def _role(dev, text):
    """ble, repeater, ... or an addon build like ble+io12 / ble+button."""
    base, _, addon = text.partition("+")
    base = ROLE_ALIASES.get(base.lower(), base)
    if not addon and base in ROLE_TITLES:
        return base
    hits = [fw.role for fw in dev.firmware
            if fw.role.startswith(base + "+") and addon.lower() in fw.role.lower()]
    if len(hits) != 1:
        roles = ", ".join(fw.role for fw in dev.firmware)
        sys.exit(f"Rolle '{text}' nicht eindeutig. Vorhanden: {roles}")
    return hits[0]


def _port_key(port):
    p = next((x for x in ports.list_serial() if x.device == port), None)
    return p.key if p else None


_last = [-1]


def _bar(pct):
    n = int(pct // 2)
    if n != _last[0]:
        _last[0] = n
        sys.stdout.write(f"\r[{'#' * n}{'.' * (50 - n)}] {pct:5.1f}%")
        if pct >= 100:
            sys.stdout.write("\n")
            _last[0] = -1
        sys.stdout.flush()
