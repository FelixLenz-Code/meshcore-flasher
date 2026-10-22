"""Local firmware library: catalog + downloaded files, usable without internet.

`sync()` mirrors the device list of flasher.meshcore.io (config.json + GitHub
releases) into library/catalog.json and downloads the firmware files into
library/files/. Everything else in this module works offline.
"""

import hashlib
import json
import os
import re
import time
import urllib.request
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

FLASHER = "https://flasher.meshcore.io"
GITHUB_REPO = "meshcore-dev/MeshCore"
USER_AGENT = "Mozilla/5.0 (meshcore-flasher)"

ROLE_TITLES = {
    "companionBle": "Companion (Bluetooth)",
    "companionUsb": "Companion (USB)",
    "repeater": "Repeater",
    "roomServer": "Room Server",
    "kissRadio": "KISS-Modem",
}
# release asset role token -> catalog role, for boards missing from config.json
ASSET_ROLES = {
    "companion_radio_ble": "companionBle",
    "companion_radio_usb": "companionUsb",
    "repeater": "repeater",
    "Repeater": "repeater",
    "room_server": "roomServer",
    "kiss_modem": "kissRadio",
}
ASSET_RE = re.compile(
    r"^(?P<board>.+?)_(?P<role>" + "|".join(ASSET_ROLES) + r")"
    r"-(?P<ver>v?\d[\d.]*)-(?P<hash>[0-9a-f]{6,})(?P<suffix>-merged\.bin|\.bin|\.uf2|\.zip|\.hex)$")

TYPE_TITLES = {"esp32": "ESP32", "nrf52": "nRF52", "rp2040": "RP2040", "stm32": "STM32"}


def version_key(v: str):
    return tuple(int(x) for x in re.findall(r"\d+", v))


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def check_content(path: Path, name: str | None = None) -> str | None:
    """Returns an error text if the file is obviously not a firmware image
    (e.g. an HTML error page saved under a .bin name)."""
    name = (name or path.name).lower()
    size = path.stat().st_size
    if size < 1024:
        return f"Datei ist nur {size} Bytes groß"
    with open(path, "rb") as f:
        head = f.read(0x1010)
    if name.endswith(".bin"):
        # ESP images start with 0xE9, ARM ones with a vector table or RP2040 boot2;
        # what must not get through is a text/HTML error page
        if head.lstrip()[:1] in (b"<", b"{") or head[:64].isascii() and head[:64].decode().isprintable():
            return "kein Firmware-Image (Textdatei)"
        return None
    if name.endswith(".uf2"):
        return None if head[:8] == b"UF2\nWQ]\x9e" else "keine UF2-Datei"
    if name.endswith(".zip"):
        try:
            with zipfile.ZipFile(path) as z:
                if z.testzip() is not None or "manifest.json" not in z.namelist():
                    return "kein gültiges DFU-Paket"
        except zipfile.BadZipFile:
            return "kein gültiges ZIP"
        return None
    if name.endswith(".hex"):
        return None if head[:1] == b":" else "keine Intel-HEX-Datei"
    return None


@dataclass
class FwFile:
    type: str  # flash | flash-wipe | flash-update | download
    name: str
    size: int = 0
    sha256: str = ""


@dataclass
class FwVersion:
    version: str
    notes: str
    files: list[FwFile]


@dataclass
class Firmware:
    role: str
    versions: list[FwVersion]  # newest first
    label: str = ""  # set for addon builds

    @property
    def title(self) -> str:
        return self.label or ROLE_TITLES.get(self.role, self.role)


@dataclass
class Device:
    id: str
    name: str
    maker: str
    type: str  # esp32 | nrf52 | rp2040 | stm32
    erase: str | None = None
    firmware: list[Firmware] = field(default_factory=list)
    extra: bool = False  # not on flasher.meshcore.io, derived from release file names

    def role(self, role: str) -> Firmware | None:
        return next((f for f in self.firmware if f.role == role), None)


class Library:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.files_dir = self.root / "files"
        self.catalog_path = self.root / "catalog.json"
        self.devices: list[Device] = []
        self.synced = None
        self.load()

    # --- offline part ---

    def load(self):
        self.devices = []
        if not self.catalog_path.exists():
            return
        data = json.loads(self.catalog_path.read_text())
        self.synced = data.get("synced")
        for d in data["devices"]:
            fws = [Firmware(f["role"], [FwVersion(v["version"], v.get("notes", ""),
                                                  [FwFile(**x) for x in v["files"]])
                                        for v in f["versions"]])
                   for f in d["firmware"]]
            self.devices.append(Device(d["id"], d["name"], d["maker"], d["type"], d.get("erase"),
                                       fws, d.get("extra", False)))
        self._load_addons()

    def _load_addons(self):
        """Own builds from mcflash.addons, shown as extra firmware of their device."""
        try:
            data = json.loads((self.root / "addons.json").read_text())
        except (OSError, ValueError):
            return
        for entry in data.values():
            dev = next((d for d in self.devices if d.id == entry["device"]), None)
            if not dev:
                continue
            for f in entry["firmware"]:
                vers = [FwVersion(v["version"], v.get("notes", ""), [FwFile(**x) for x in v["files"]])
                        for v in f["versions"]]
                dev.firmware.append(Firmware(f["role"], vers, f["label"]))

    def device(self, key: str) -> Device | None:
        """Finds a device by id, exact name or unique substring."""
        for d in self.devices:
            if key in (d.id, d.name):
                return d
        hits = [d for d in self.devices if key.lower() in d.name.lower() or key.lower() in d.id]
        return hits[0] if len(hits) == 1 else None

    def path(self, name: str) -> Path:
        return self.files_dir / name

    def have(self, f: FwFile) -> bool:
        p = self.path(f.name)
        return p.exists() and (not f.size or p.stat().st_size == f.size)

    def verify(self, f: FwFile) -> str | None:
        """Error text, or None if the local copy is intact."""
        p = self.path(f.name)
        if not p.exists():
            return f"{f.name} ist nicht in der Bibliothek (erst online synchronisieren)"
        if f.sha256 and sha256_file(p) != f.sha256:
            return f"{f.name} ist beschädigt (Prüfsumme falsch), bitte neu synchronisieren"
        return check_content(p)

    def file_for(self, f: FwFile) -> Path:
        return self.path(f.name)

    # --- online part ---

    def sync(self, *, devices: list[str] | None = None, versions: int = 1, download: bool = True,
             log=print, progress=None, cancelled=lambda: False):
        """Updates catalog.json and downloads the firmware files.

        devices: only download files for devices whose name contains one of these strings.
        versions: number of newest versions per role to keep in the catalog/download.
        """
        self.root.mkdir(parents=True, exist_ok=True)
        self.files_dir.mkdir(exist_ok=True)

        log("Lade Geräteliste von flasher.meshcore.io …")
        config = _get_json(f"{FLASHER}/config.json")
        releases = _get_json(f"{FLASHER}/releases?repo=github")
        log("Lade Prüfsummen von GitHub …")
        digests = _github_digests()

        devices_out = _build_catalog(config, releases, digests, versions, log)
        self._keep_local_versions(devices_out)
        self._write_catalog(devices_out)
        self.load()
        log(f"Katalog: {len(self.devices)} Geräte")
        if not download:
            return

        wanted = []
        for d in self.devices:
            if devices and not any(s.lower() in d.name.lower() or s.lower() in d.id for s in devices):
                continue
            files = [f for fw in d.firmware for v in fw.versions for f in v.files]
            if d.erase:
                files.append(FwFile("erase", d.erase))
            wanted += [f for f in files if f.name not in {w.name for w in wanted}]

        todo = [f for f in wanted if not (self.have(f) and (not f.sha256 or self.verify(f) is None))]
        total = sum(f.size for f in todo)
        log(f"{len(wanted)} Dateien gewünscht, {len(todo)} fehlen ({total / 1e6:.0f} MB)")
        done_bytes = 0
        failed = []
        for i, f in enumerate(todo, 1):
            if cancelled():
                log("Abgebrochen.")
                break
            log(f"[{i}/{len(todo)}] {f.name}")
            try:
                _download(f, self.files_dir, digests)
            except Exception as e:  # noqa: BLE001 - keep going with the other files
                log(f"   FEHLER: {e}")
                failed.append(f.name)
            done_bytes += f.size
            if progress and total:
                progress(100 * done_bytes / total)
        # erase/static files have no size/digest in the catalog until downloaded
        self._fill_local_hashes()
        if failed:
            log(f"{len(failed)} Dateien fehlgeschlagen, Synchronisieren einfach wiederholen.")
        else:
            log("Bibliothek ist vollständig.")

    def _keep_local_versions(self, devices_out):
        """Older versions that were downloaded before stay flashable offline."""
        new = {d["id"]: d for d in devices_out}
        for old in self.devices:
            d = new.get(old.id)
            if not d:
                continue
            for ofw in old.firmware:
                fw = next((f for f in d["firmware"] if f["role"] == ofw.role), None)
                if not fw:
                    continue
                have = {v["version"] for v in fw["versions"]}
                for v in ofw.versions:
                    if v.version not in have and v.files and all(self.have(f) for f in v.files):
                        fw["versions"].append({"version": v.version, "notes": v.notes,
                                               "files": [f.__dict__ for f in v.files]})
                fw["versions"].sort(key=lambda v: version_key(v["version"]), reverse=True)

    def _write_catalog(self, devices_out):
        data = {"synced": time.strftime("%Y-%m-%d %H:%M"), "source": FLASHER, "devices": devices_out}
        tmp = self.catalog_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False))
        os.replace(tmp, self.catalog_path)

    def _fill_local_hashes(self):
        data = json.loads(self.catalog_path.read_text())
        for d in data["devices"]:
            for fw in d["firmware"]:
                for v in fw["versions"]:
                    for f in v["files"]:
                        p = self.path(f["name"])
                        if not f.get("sha256") and p.exists():
                            f["size"] = p.stat().st_size
                            f["sha256"] = sha256_file(p)
        self._write_catalog(data["devices"])
        self.load()

    def status(self) -> tuple[int, int]:
        """(files present, files listed) for the newest version of every role."""
        listed = present = 0
        for d in self.devices:
            for fw in d.firmware:
                if fw.versions:
                    for f in fw.versions[0].files:
                        listed += 1
                        present += self.have(f)
        return present, listed


def _get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def _github_digests() -> dict:
    """{asset name: sha256} for all release assets (GitHub publishes digests)."""
    out = {}
    try:
        for page in range(1, 6):
            rels = _get_json(f"https://api.github.com/repos/{GITHUB_REPO}/releases?per_page=100&page={page}")
            for r in rels:
                for a in r["assets"]:
                    d = a.get("digest") or ""
                    if d.startswith("sha256:"):
                        out[a["name"]] = (d[7:], a["size"], a["browser_download_url"])
            if len(rels) < 100:
                break
    except Exception:  # noqa: BLE001 - rate limit etc.; files are still content-checked
        pass
    return out


def _build_catalog(config, releases, digests, versions, log):
    by_name: dict[str, dict] = {}
    covered = set()  # release asset names that some config entry uses

    for dev in config["device"]:
        if dev["type"] not in ("esp32", "nrf52", "noflash"):
            continue
        fws = []
        for fw in dev["firmware"]:
            if fw.get("class") != "community" or fw["role"] not in ROLE_TITLES:
                continue
            vers = []
            src_key = next((k for k in fw if k.startswith("github")), None)
            if src_key == "github" and fw[src_key].get("files"):
                src = fw[src_key]
                for rel in releases:
                    if rel["type"] != src["type"]:
                        continue
                    files = []
                    for ftype, pattern in src["files"].items():
                        rx = re.compile(pattern)
                        match = next((a for a in rel["files"] if rx.search(a["name"])), None)
                        if match:
                            covered.add(match["name"])
                            files.append(_fwfile(ftype, match["name"], digests))
                    if files:
                        vers.append({"version": rel["version"], "notes": rel.get("notes", ""), "files": files})
            elif not src_key:
                for v, body in fw.get("version", {}).items():
                    vers.append({"version": v, "notes": body.get("notes", ""),
                                 "files": [_fwfile(f["type"], f["name"], digests) for f in body["files"]]})
            vers.sort(key=lambda v: version_key(v["version"]), reverse=True)
            if vers:
                fws.append({"role": fw["role"], "versions": vers[:versions]})
        if not fws:
            continue
        dtype = "rp2040" if dev["type"] == "noflash" else dev["type"]
        entry = by_name.setdefault(dev["name"], {
            "id": slug(dev["name"]), "name": dev["name"],
            "maker": config.get("maker", {}).get(dev.get("maker"), {}).get("name", dev.get("maker", "")),
            "type": dtype, "erase": dev.get("erase"), "firmware": []})
        known = {f["role"] for f in entry["firmware"]}
        entry["firmware"] += [f for f in fws if f["role"] not in known]

    # boards that have release files but no entry on flasher.meshcore.io
    # (e.g. STM32WL boards like RAK3172 / Wio-E5), or roles missing there.
    # Only the newest release per type, so renamed/dropped boards don't linger.
    board_owner = {}  # asset board prefix -> config device name
    for name, entry in by_name.items():
        for fw in entry["firmware"]:
            for v in fw["versions"]:
                for f in v["files"]:
                    m = ASSET_RE.match(f["name"])
                    if m:
                        board_owner.setdefault(m["board"], name)
    newest = {}
    for rel in releases:
        if rel["type"] not in newest or version_key(rel["version"]) > version_key(newest[rel["type"]]["version"]):
            newest[rel["type"]] = rel
    extra: dict[tuple, dict] = {}
    for rel in newest.values():
        for a in rel["files"]:
            m = ASSET_RE.match(a["name"])
            if not m or a["name"] in covered:
                continue
            key = (m["board"], ASSET_ROLES[m["role"]])
            extra.setdefault(key, {"version": rel["version"], "notes": rel.get("notes", ""), "files": {}})
            extra[key]["files"][m["suffix"]] = a["name"]
    extras_by_board: dict[str, dict] = {}
    for (board, role), rel in extra.items():
        sfx = set(rel["files"])
        if ".zip" in sfx:
            dtype, ft = "nrf52", [("flash", ".zip"), ("download", ".uf2")]
        elif "-merged.bin" in sfx:
            dtype, ft = "esp32", [("flash-wipe", "-merged.bin"), ("flash-update", ".bin")]
        elif ".hex" in sfx:
            dtype, ft = "stm32", [("flash", ".hex"), ("download", ".bin")]
        elif ".uf2" in sfx and ".bin" in sfx:
            dtype, ft = "rp2040", [("flash", ".uf2"), ("download", ".bin")]
        elif ".uf2" in sfx:
            dtype, ft = "nrf52", [("download", ".uf2")]
        else:
            continue
        fw = {"role": role, "versions": [{"version": rel["version"], "notes": rel["notes"], "files": [
            _fwfile(t, rel["files"][s], digests) for t, s in ft if s in rel["files"]]}]}
        owner = by_name.get(board_owner.get(board))
        if owner and owner["type"] == dtype:
            if role not in {f["role"] for f in owner["firmware"]}:
                owner["firmware"].append(fw)
            continue
        name = board.replace("_", " ") + " *"
        e = extras_by_board.setdefault(name, {"id": slug(board), "name": name, "maker": "",
                                              "type": dtype, "erase": None, "firmware": [], "extra": True})
        e["firmware"].append(fw)
    if extras_by_board:
        log(f"{len(extras_by_board)} zusätzliche Boards nur aus den Release-Dateien (mit * markiert)")

    out = sorted([*by_name.values(), *extras_by_board.values()], key=lambda d: d["name"].lower())
    order = list(ROLE_TITLES)
    for d in out:
        d["firmware"].sort(key=lambda f: order.index(f["role"]))
    return out


def _fwfile(ftype, name, digests):
    sha, size, _url = digests.get(name, ("", 0, ""))
    return {"type": ftype, "name": name, "size": size, "sha256": sha}


def _download(f: FwFile, dest_dir: Path, digests):
    urls = []
    if f.name in digests:
        urls.append(digests[f.name][2])
    urls.append(f"{FLASHER}/firmware/{f.name}")
    last = None
    for url in urls:
        try:
            _download_url(url, dest_dir / f.name, f)
            return
        except Exception as e:  # noqa: BLE001
            last = e
    raise last


def _download_url(url, dest: Path, f: FwFile):
    tmp = dest.with_name(dest.name + ".part")
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    h = hashlib.sha256()
    with urllib.request.urlopen(req, timeout=60) as r, open(tmp, "wb") as out:
        for chunk in iter(lambda: r.read(1 << 16), b""):
            out.write(chunk)
            h.update(chunk)
        out.flush()
        os.fsync(out.fileno())
    try:
        if f.sha256 and h.hexdigest() != f.sha256:
            raise ValueError("Prüfsumme stimmt nicht")
        err = check_content(tmp, dest.name)
        if err:
            raise ValueError(err)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    os.replace(tmp, dest)
