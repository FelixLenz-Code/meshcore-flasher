#!/usr/bin/env bash
# Baut das .deb-Paket.
#   packaging/build-deb.sh <version> [amd64|arm64] [ausgabeordner]
# Ergebnis: <ausgabeordner>/meshcore-flasher_<version>_<arch>.deb
set -euo pipefail
SRC="$(cd "$(dirname "$0")/.." && pwd)"
VERSION="${1:?Version fehlt, z.B. 0.1.0}"
VERSION="${VERSION#v}"
ARCH="${2:-$(dpkg --print-architecture)}"
OUT="${3:-$SRC/dist}"
case "$ARCH" in amd64) PYARCH=x86_64 ;; arm64) PYARCH=aarch64 ;; *) echo "Arch $ARCH?" >&2; exit 1 ;; esac

STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
APP="$STAGE/opt/meshcore-flasher"
mkdir -p "$APP" "$STAGE/usr/bin" "$STAGE/usr/share/applications" "$STAGE/usr/lib/udev/rules.d" \
         "$STAGE/usr/share/doc/meshcore-flasher" "$STAGE/DEBIAN"

cp -r "$SRC/mcflash" "$SRC/addons" "$APP/"
cp "$SRC/meshcore-flasher" "$SRC/99-meshcore-flasher.rules" "$SRC/LICENSE" "$APP/"
find "$APP" -name __pycache__ -prune -exec rm -rf {} +
sed -i "s/^__version__ = .*/__version__ = \"$VERSION\"/" "$APP/mcflash/__init__.py"
touch "$APP/.system-install"
"$SRC/packaging/fetch-wheels.sh" "$APP/wheels" "$PYARCH"

ln -s /opt/meshcore-flasher/meshcore-flasher "$STAGE/usr/bin/meshcore-flasher"
cp "$SRC/99-meshcore-flasher.rules" "$STAGE/usr/lib/udev/rules.d/"
sed "s|@DIR@/meshcore-flasher|/usr/bin/meshcore-flasher|" "$SRC/meshcore-flasher.desktop.in" \
    > "$STAGE/usr/share/applications/meshcore-flasher.desktop"
cp "$SRC/LICENSE" "$STAGE/usr/share/doc/meshcore-flasher/copyright"

SIZE=$(du -sk --exclude=DEBIAN "$STAGE" | cut -f1)
sed -e "s/@VERSION@/$VERSION/" -e "s/@ARCH@/$ARCH/" -e "s/@SIZE@/$SIZE/" \
    "$SRC/packaging/debian/control" > "$STAGE/DEBIAN/control"
install -m 755 "$SRC/packaging/debian/postinst" "$SRC/packaging/debian/prerm" "$STAGE/DEBIAN/"

mkdir -p "$OUT"
dpkg-deb --root-owner-group -Zxz --build "$STAGE" "$OUT/meshcore-flasher_${VERSION}_${ARCH}.deb"
