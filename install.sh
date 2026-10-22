#!/usr/bin/env bash
# Installation aus dem Quellordner: legt das venv an und installiert esptool und
# adafruit-nrfutil aus ./wheels (offline) oder, falls der Ordner fehlt, aus dem Internet.
# PyQt6 kommt vom System (Paket python3-pyqt6).
# Die udev-Regel gegen ModemManager wird standardmäßig mit installiert (sudo).
#   ./install.sh --no-udev  ohne udev-Regel
#   ./install.sh --udev     nur die udev-Regel installieren/aktualisieren
#   ./install.sh --pio    installiert PlatformIO für Addons (Internet)
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"
RULES=/etc/udev/rules.d/99-meshcore-flasher.rules
install_udev() {
    if cmp -s 99-meshcore-flasher.rules "$RULES"; then
        echo "udev-Regel ist aktuell."
    elif sudo install -m 644 99-meshcore-flasher.rules "$RULES" \
            && sudo udevadm control --reload-rules \
            && sudo udevadm trigger --subsystem-match=tty --action=add; then
        echo "udev-Regel installiert (ModemManager lässt MeshCore-Geräte in Ruhe)."
    else
        echo "udev-Regel nicht installiert. Später: ./install.sh --udev" >&2
    fi
}
if [ "${1:-}" = "--udev" ]; then
    install_udev
    exit 0
fi
if [ "${1:-}" = "--pio" ]; then
    # PlatformIO zum Bauen der Addons (braucht einmalig Internet)
    [ -d .pio-venv ] || python3 -m venv .pio-venv
    .pio-venv/bin/pip install -q platformio
    .pio-venv/bin/pio --version
    exit 0
fi
python3 -c "import PyQt6.QtWidgets" 2>/dev/null || {
    echo "PyQt6 fehlt (nur für die Oberfläche): sudo apt install python3-pyqt6" >&2; }
[ -d .venv ] || python3 -m venv --system-site-packages .venv
if ls wheels/*.whl >/dev/null 2>&1; then
    .venv/bin/pip install -q --no-index --find-links wheels esptool adafruit-nrfutil
else
    # frischer git-Klon ohne wheels/: einmalig aus dem Internet (packaging/fetch-wheels.sh legt sie an)
    .venv/bin/pip install -q esptool adafruit-nrfutil
fi
mkdir -p ~/.local/share/applications
sed "s|@DIR@|$DIR|g" meshcore-flasher.desktop.in > ~/.local/share/applications/meshcore-flasher.desktop
[ "${1:-}" = "--no-udev" ] || install_udev
id -nG | grep -qw dialout || echo "Hinweis: sudo usermod -aG dialout $USER  (danach neu anmelden)"
echo "Installiert. Start: $DIR/meshcore-flasher  (oder über das Anwendungsmenü)"
echo "Erster Schritt mit Internet: 'Neueste Firmware holen' bzw. ./meshcore-flasher sync"
