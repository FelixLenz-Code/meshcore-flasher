"""Where things live. A git checkout keeps everything next to the code; a system
install (.deb in /opt, marked by .system-install) keeps the library, addon builds
and PlatformIO in the user's data directory."""

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def system_install() -> bool:
    return (ROOT / ".system-install").exists() or not os.access(ROOT, os.W_OK)


def data_dir() -> Path:
    if os.environ.get("MCFLASH_DATA"):
        return Path(os.environ["MCFLASH_DATA"])
    if not system_install():
        return ROOT
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "meshcore-flasher"


def library_root() -> Path:
    return data_dir() / "library"


def addon_dirs() -> list[Path]:
    """Bundled addons plus the user's own (data dir), the latter win on equal names."""
    dirs = [ROOT / "addons"]
    if data_dir() != ROOT:
        dirs.append(data_dir() / "addons")
    return dirs
