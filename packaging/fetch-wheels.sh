#!/usr/bin/env bash
# Lädt esptool, adafruit-nrfutil und alle Abhängigkeiten als Wheels für mehrere
# Python-Versionen herunter, damit die Installation danach ohne Internet klappt.
#   packaging/fetch-wheels.sh <zielordner> [x86_64|aarch64]
set -euo pipefail
DEST="${1:?Zielordner fehlt}"
ARCH="${2:-$(uname -m)}"
PYVERSIONS="${PYVERSIONS:-3.11 3.12 3.13 3.14}"
mkdir -p "$DEST"
# Manche Pakete (z.B. esptool 5.x) gibt es nur als Quellpaket: reine Python-Pakete
# hier einmal zu Wheels bauen, die Schleife unten findet sie über --find-links.
python3 -m pip wheel -q --wheel-dir "$DEST" esptool adafruit-nrfutil
for py in $PYVERSIONS; do
    python3 -m pip download -q --dest "$DEST" --find-links "$DEST" --only-binary=:all: \
        --python-version "$py" --implementation cp \
        --platform "manylinux2014_$ARCH" --platform "manylinux_2_28_$ARCH" \
        --platform "manylinux_2_34_$ARCH" --platform linux_"$ARCH" \
        esptool adafruit-nrfutil
done
ls "$DEST" | wc -l | xargs echo "Wheels:"
