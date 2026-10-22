"""Remembered choices: which device/role was flashed on which USB device."""

import json
import os
from pathlib import Path

PATH = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "meshcore-flasher" / "state.json"


def load() -> dict:
    try:
        return json.loads(PATH.read_text())
    except (OSError, ValueError):
        return {}


def save(data: dict):
    PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1))
    os.replace(tmp, PATH)


def remember(port_key: str, device_id: str, role: str):
    data = load()
    data.setdefault("ports", {})[port_key] = {"device": device_id, "role": role}
    data["last"] = {"device": device_id, "role": role}
    save(data)


def recall(port_key: str | None) -> dict | None:
    data = load()
    return data.get("ports", {}).get(port_key) if port_key else None
