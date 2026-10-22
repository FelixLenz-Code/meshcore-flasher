"""Addons: own patches on top of a MeshCore release, built once with PlatformIO
and then flashed offline like any other firmware.

addons/<name>/addon.json  device id, title and the PlatformIO envs to build
addons/<name>/patch.diff  `git diff` against the MeshCore source

Builds land in library/files, their catalog entries in library/addons.json
(kept apart from catalog.json so a sync doesn't touch them).
"""

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from .catalog import ROLE_TITLES, Library, sha256_file, version_key
from .flasher import FlashError
from .paths import ROOT, addon_dirs, data_dir

GITHUB_URL = "https://github.com/meshcore-dev/MeshCore.git"
LOCAL_CHECKOUT = ROOT.parent / "MeshCore"  # the user's own clone, saves the download
KEEP_VERSIONS = 3


def available() -> dict[str, dict]:
    out = {}
    for base in addon_dirs():
        for d in sorted(base.glob("*/addon.json")):
            data = json.loads(d.read_text())
            data["name"] = d.parent.name
            data["patch"] = d.parent / "patch.diff"
            out[data["name"]] = data
    return dict(sorted(out.items()))


def setup_pio(run, log) -> str:
    """Installs PlatformIO into <data dir>/.pio-venv (needs internet once)."""
    venv = data_dir() / ".pio-venv"
    if not (venv / "bin" / "pip").exists():
        log(f"Lege {venv} an …")
        venv.parent.mkdir(parents=True, exist_ok=True)
        rc, out = run([sys.executable, "-m", "venv", str(venv)])
        if rc != 0:
            raise FlashError("venv konnte nicht angelegt werden (Paket python3-venv installiert?)")
    rc, _out = run([str(venv / "bin" / "pip"), "install", "-q", "platformio"])
    if rc != 0:
        raise FlashError("PlatformIO konnte nicht installiert werden (Internet?)")
    return str(venv / "bin" / "pio")


def find_pio() -> str | None:
    for cand in (data_dir() / ".pio-venv/bin/pio", ROOT / ".pio-venv/bin/pio",
                 Path.home() / ".platformio/penv/bin/pio"):
        if cand.exists():
            return str(cand)
    return shutil.which("pio") or shutil.which("platformio")


def built_version(lib: Library, addon: dict) -> str | None:
    """Newest version this addon was built for, None if never."""
    data = _load_registry(lib)
    vers = [v["version"] for fw in data.get(addon["name"], {}).get("firmware", []) for v in fw["versions"]]
    return max(vers, key=version_key) if vers else None


def target_version(lib: Library, addon: dict, build: dict) -> str:
    dev = lib.device(addon["device"])
    fw = dev.role(build["role"]) if dev else None
    if not fw or not fw.versions:
        raise FlashError(f"{addon['device']} / {build['role']} ist nicht im Katalog, erst synchronisieren")
    return fw.versions[0].version


def outdated(lib: Library) -> list[tuple[str, str | None, str]]:
    """(addon, built version, newest release version) for addons that need a (re)build."""
    out = []
    for name, addon in available().items():
        try:
            want = max((target_version(lib, addon, b) for b in addon["builds"]), key=version_key)
        except FlashError:
            continue
        have = built_version(lib, addon)
        if have is None or version_key(have) < version_key(want):
            out.append((name, have, want))
    return out


def build(lib: Library, name: str, run, log, version: str | None = None):
    """Builds addon `name` for the newest catalog version (or `version`).
    run(cmd, cwd=...) -> (returncode, output) streams the tool output."""
    addon = available().get(name)
    if not addon:
        raise FlashError(f"Addon {name} gibt es nicht")
    pio = find_pio()
    if not pio:
        raise FlashError("PlatformIO fehlt. Einmalig mit Internet: meshcore-flasher addons setup")
    src = _source_repo(lib, run, log)

    for b in addon["builds"]:
        ver = version or target_version(lib, addon, b)
        tag = f"{b['release']}-{ver}"
        log(f"━ {addon['title']}: {b['env']} auf Basis von {tag}")
        if _git(src, "rev-parse", "-q", "--verify", f"refs/tags/{tag}").returncode != 0:
            log("Tag fehlt lokal, hole Tags von GitHub …")
            _git(src, "fetch", "-q", "--tags", GITHUB_URL)
            if _git(src, "rev-parse", "-q", "--verify", f"refs/tags/{tag}").returncode != 0:
                raise FlashError(f"Release-Tag {tag} nicht gefunden (Internet nötig?)")
        r = _git(src, "checkout", "-q", "-f", tag)
        if r.returncode != 0:
            raise FlashError(f"git checkout {tag} fehlgeschlagen:\n{r.stderr}")
        r = _git(src, "apply", str(addon["patch"]))
        if r.returncode != 0:
            raise FlashError(f"Patch passt nicht mehr auf {tag}, er muss angepasst werden:\n{r.stderr.strip()}")

        short = _git(src, "rev-parse", "--short=7", "HEAD").stdout.strip()
        fw_version = f"{ver}-{short}+{name.split('-')[-1]}"
        env = dict(os.environ)
        env["PLATFORMIO_BUILD_FLAGS"] = (f"-DFIRMWARE_BUILD_DATE='\"{time.strftime('%d-%b-%Y')}\"' "
                                         f"-DFIRMWARE_VERSION='\"{fw_version}\"'")
        for args in (["run", "-e", b["env"]], ["run", "-t", "mergebin", "-e", b["env"]]):
            rc, _out = run([pio, *args], cwd=src, env=env)
            if rc != 0:
                raise FlashError(f"Build fehlgeschlagen (pio {' '.join(args)}). Beim allerersten Build "
                                 "braucht PlatformIO Internet für Bibliotheken und Toolchain.")
        _git(src, "checkout", "-q", "-f", tag)  # leave the source clean for the next patch

        out_dir = src / ".pio" / "build" / b["env"]
        base = f"{b['env']}-{ver}-{short}+{name}"
        files = []
        for suffix, ftype in (("-merged.bin", "flash-wipe"), (".bin", "flash-update")):
            produced = out_dir / ("firmware-merged.bin" if suffix == "-merged.bin" else "firmware.bin")
            if not produced.exists():
                raise FlashError(f"{produced.name} wurde nicht erzeugt")
            dest = lib.files_dir / f"{base}{suffix}"
            lib.files_dir.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(produced, dest)
            files.append({"type": ftype, "name": dest.name, "size": dest.stat().st_size,
                          "sha256": sha256_file(dest)})
            log(f"→ {dest.name}")
        _register(lib, addon, b["role"], ver, fw_version, files)
    lib.load()
    log(f"Addon {addon['title']} ist gebaut und in der Bibliothek.")


def _source_repo(lib: Library, run, log) -> Path:
    src = lib.root / "src" / "MeshCore"
    if (src / ".git").exists():
        return src
    src.parent.mkdir(parents=True, exist_ok=True)
    origin = str(LOCAL_CHECKOUT) if (LOCAL_CHECKOUT / ".git").exists() else GITHUB_URL
    log(f"Lege Quellcode-Kopie an (von {origin}) …")
    rc, out = run(["git", "clone", "-q", "--no-checkout", origin, str(src)])
    if rc != 0:
        raise FlashError("git clone fehlgeschlagen:\n" + out[-500:])
    _git(src, "remote", "set-url", "origin", GITHUB_URL)
    # reuse the libraries the user's checkout already downloaded (offline builds)
    libdeps = LOCAL_CHECKOUT / ".pio" / "libdeps"
    if libdeps.is_dir() and not (src / ".pio" / "libdeps").exists():
        (src / ".pio").mkdir(exist_ok=True)
        shutil.copytree(libdeps, src / ".pio" / "libdeps", symlinks=True)
    return src


def _git(repo: Path, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)


def _load_registry(lib: Library) -> dict:
    p = lib.root / "addons.json"
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return {}


def _register(lib, addon, role, version, fw_version, files):
    data = _load_registry(lib)
    entry = data.setdefault(addon["name"], {"device": addon["device"], "title": addon["title"], "firmware": []})
    entry["device"], entry["title"] = addon["device"], addon["title"]
    fw = next((f for f in entry["firmware"] if f["base_role"] == role), None)
    if fw is None:
        fw = {"role": f"{role}+{addon['name']}", "base_role": role,
              "label": f"{ROLE_TITLES.get(role, role)} + {addon['title']}", "versions": []}
        entry["firmware"].append(fw)
    fw["versions"] = [v for v in fw["versions"] if v["version"] != version]
    fw["versions"].append({"version": version, "notes": f"{addon.get('description', '')} ({fw_version})",
                           "files": files})
    fw["versions"].sort(key=lambda v: version_key(v["version"]), reverse=True)
    fw["versions"] = fw["versions"][:KEEP_VERSIONS]
    p = lib.root / "addons.json"
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False))
    os.replace(tmp, p)
