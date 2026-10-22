"""PyQt6 UI for the MeshCore flasher."""

import sys
from pathlib import Path

from PyQt6.QtCore import QThread, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QFontDatabase, QIcon, QPainter, QPixmap
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QCompleter, QFileDialog, QFormLayout, QHBoxLayout,
    QLabel, QMainWindow, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QVBoxLayout,
    QSizePolicy, QWidget,
)

from . import __version__, addons, doctor, ports, state
from .catalog import TYPE_TITLES, Library
from .paths import addon_dirs, library_root
from .flasher import Flasher, FlashError, guess_type

HINTS = {
    "esp32": "ESP32: wird über esptool geflasht. Klappt die Verbindung nicht, BOOT gedrückt halten, "
             "kurz RESET drücken, BOOT loslassen.",
    "nrf52": "nRF52: Das Gerät wird automatisch in den Bootloader gesetzt. Falls das nicht klappt: "
             "zweimal schnell Reset drücken (LED pulsiert) und „schon im Bootloader“ ankreuzen.",
    "rp2040": "RP2040: Kopiert die UF2-Datei auf das Bootloader-Laufwerk. Ohne MeshCore-Firmware: "
              "BOOTSEL gedrückt halten und anstecken.",
    "stm32": "STM32 (RAK3172, Wio-E5): braucht ST-Link (stlink-tools) oder UART-Bootloader (stm32flash).",
}


def app_icon() -> QIcon:
    pm = QPixmap(64, 64)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(QColor("#b5532a"))
    p.setPen(Qt.PenStyle.NoPen)
    p.drawRoundedRect(2, 2, 60, 60, 14, 14)
    p.setPen(QColor("white"))
    f = QFont()
    f.setBold(True)
    f.setPixelSize(34)
    p.setFont(f)
    p.drawText(pm.rect(), Qt.AlignmentFlag.AlignCenter, "⚡")
    p.end()
    return QIcon(pm)


class Worker(QThread):
    log = pyqtSignal(str)
    progress = pyqtSignal(float)
    done = pyqtSignal(bool, str)

    def __init__(self, fn):
        super().__init__()
        self.fn = fn
        self.flasher = Flasher(log=self.log.emit, progress=self.progress.emit)
        self.cancelled = False

    def cancel(self):
        self.cancelled = True
        self.flasher.cancel()

    def run(self):
        try:
            self.fn(self)
            self.done.emit(True, "")
        except FlashError as e:
            self.done.emit(False, str(e))
        except Exception as e:  # noqa: BLE001 - show anything in the UI instead of dying
            self.done.emit(False, f"{type(e).__name__}: {e}")


class Main(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"MeshCore Flasher {__version__}")
        self.setWindowIcon(app_icon())
        self.lib = Library(library_root())
        self.worker: Worker | None = None
        self.port_list: list[ports.Port] = []

        w = QWidget()
        self.setCentralWidget(w)
        v = QVBoxLayout(w)

        top = QHBoxLayout()
        self.lib_label = QLabel()
        top.addWidget(self.lib_label, 1)
        self.sync_btn = QPushButton("🌐 Neueste Firmware holen")
        self.sync_btn.setToolTip("Lädt Geräteliste und neueste Firmware von flasher.meshcore.io / GitHub. "
                                 "Braucht Internet, nur auf Knopfdruck.")
        self.sync_btn.clicked.connect(self.sync)
        top.addWidget(self.sync_btn)
        self.addon_btn = QPushButton("🧩 Addons")
        self.addon_btn.setToolTip("Eigene Patches (Ordner addons/) auf die neueste Release bauen")
        self.addon_btn.clicked.connect(self.build_addons)
        top.addWidget(self.addon_btn)
        b = QPushButton("🩺 System prüfen")
        b.clicked.connect(self.run_doctor)
        top.addWidget(b)
        v.addLayout(top)

        form = QFormLayout()
        self.device_cb = QComboBox()
        self.device_cb.setEditable(True)
        self.device_cb.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.device_cb.completer().setFilterMode(Qt.MatchFlag.MatchContains)
        self.device_cb.completer().setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
        self.device_cb.lineEdit().setPlaceholderText("Gerät suchen …")
        self.device_cb.currentIndexChanged.connect(self.device_changed)
        form.addRow("Gerät:", self.device_cb)
        self.role_cb = QComboBox()
        self.role_cb.currentIndexChanged.connect(self.role_changed)
        form.addRow("Firmware:", self.role_cb)
        self.version_cb = QComboBox()
        self.version_cb.currentIndexChanged.connect(self.update_state)
        form.addRow("Version:", self.version_cb)

        prow = QHBoxLayout()
        self.port_cb = QComboBox()
        self.port_cb.currentIndexChanged.connect(self.port_changed)
        prow.addWidget(self.port_cb, 1)
        b = QPushButton("↻")
        b.setFixedWidth(36)
        b.setToolTip("Ports neu einlesen")
        b.clicked.connect(lambda: self.refresh_ports(force=True))
        prow.addWidget(b)
        form.addRow("Port:", prow)

        self.wipe_chk = QCheckBox("Komplett neu installieren (Flash löschen, auch Einstellungen und Pairings)")
        form.addRow("", self.wipe_chk)
        self.method_cb = QComboBox()
        self.method_cb.addItem("Serial-DFU (empfohlen)", "auto")
        self.method_cb.addItem("UF2-Laufwerk", "uf2")
        self.method_lbl = QLabel("Methode:")
        form.addRow(self.method_lbl, self.method_cb)
        self.boot_chk = QCheckBox("Gerät ist schon im Bootloader / DFU-Modus")
        form.addRow("", self.boot_chk)
        # combos use the full window width, popups as wide as their longest entry
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        for cb in (self.device_cb, self.role_cb, self.version_cb, self.port_cb, self.method_cb):
            cb.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            cb.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
            cb.view().setTextElideMode(Qt.TextElideMode.ElideNone)
        v.addLayout(form)

        self.hint = QLabel()
        self.hint.setWordWrap(True)
        self.hint.setStyleSheet("color: palette(placeholder-text);")
        v.addWidget(self.hint)

        btns = QHBoxLayout()
        self.flash_btn = QPushButton("⚡ Flashen")
        f = self.flash_btn.font()
        f.setBold(True)
        self.flash_btn.setFont(f)
        self.flash_btn.setMinimumHeight(36)
        self.flash_btn.clicked.connect(self.flash)
        btns.addWidget(self.flash_btn, 2)
        self.file_btn = QPushButton("📄 Eigene Datei …")
        self.file_btn.setToolTip("Selbst gebaute Firmware (.bin, -merged.bin, .zip, .uf2, .hex) flashen")
        self.file_btn.clicked.connect(self.flash_custom)
        btns.addWidget(self.file_btn, 1)
        self.erase_btn = QPushButton("🧹 nRF52 löschen")
        self.erase_btn.setToolTip("Flasht die offizielle Lösch-Firmware (entfernt Einstellungen und Dateisystem)")
        self.erase_btn.clicked.connect(self.erase)
        btns.addWidget(self.erase_btn, 1)
        self.cancel_btn = QPushButton("Abbrechen")
        self.cancel_btn.clicked.connect(self.cancel)
        self.cancel_btn.setEnabled(False)
        btns.addWidget(self.cancel_btn)
        v.addLayout(btns)

        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.setTextVisible(False)
        v.addWidget(self.bar)
        self.logw = QPlainTextEdit()
        self.logw.setReadOnly(True)
        self.logw.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        v.addWidget(self.logw, 1)

        self.resize(760, 680)
        self.fill_devices()
        self.refresh_ports(force=True)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh_ports)
        self.timer.start(1500)
        self.report_outdated_addons()
        QTimer.singleShot(0, self.offer_udev_fix)
        if not self.lib.devices:
            self.log("Die Bibliothek ist leer. Mit Internet einmal „Neueste Firmware holen“ klicken, "
                     "danach geht alles offline.")

    # --- library ---

    def fill_devices(self, keep=None):
        keep = keep or self.current_device_id()
        self.device_cb.blockSignals(True)
        self.device_cb.clear()
        for d in self.lib.devices:
            self.device_cb.addItem(f"{d.name}   ·  {TYPE_TITLES.get(d.type, d.type)}", d.id)
        self.device_cb.blockSignals(False)
        idx = self.device_cb.findData(keep) if keep else -1
        self.device_cb.setCurrentIndex(max(idx, 0) if self.lib.devices else -1)
        self.device_changed()
        present, listed = self.lib.status()
        if self.lib.synced:
            mark = "✓" if present == listed else "⚠"
            self.lib_label.setText(f"Bibliothek: Stand {self.lib.synced} · {len(self.lib.devices)} Geräte · "
                                   f"{mark} {present}/{listed} Dateien")
        else:
            self.lib_label.setText("Bibliothek: leer")

    def current_device(self):
        did = self.current_device_id()
        return next((d for d in self.lib.devices if d.id == did), None)

    def current_device_id(self):
        return self.device_cb.currentData() if self.device_cb.count() else None

    def device_changed(self, *_):
        d = self.current_device()
        prev = self.role_cb.currentData()
        self.role_cb.blockSignals(True)
        self.role_cb.clear()
        for fw in d.firmware if d else []:
            self.role_cb.addItem(fw.title, fw.role)
        self.role_cb.blockSignals(False)
        i = self.role_cb.findData(prev)
        self.role_cb.setCurrentIndex(max(i, 0))
        self.role_changed()

    def role_changed(self, *_):
        d = self.current_device()
        fw = d.role(self.role_cb.currentData()) if d else None
        self.version_cb.blockSignals(True)
        self.version_cb.clear()
        for ver in fw.versions if fw else []:
            have = all(self.lib.have(f) for f in ver.files)
            self.version_cb.addItem(ver.version + ("" if have else "  (nicht heruntergeladen)"), ver.version)
        self.version_cb.blockSignals(False)
        self.update_state()

    def update_state(self, *_):
        d = self.current_device()
        t = d.type if d else None
        busy = self.worker is not None
        self.wipe_chk.setVisible(t == "esp32")
        self.method_cb.setVisible(t == "nrf52")
        self.method_lbl.setVisible(t == "nrf52")
        self.boot_chk.setVisible(t in ("nrf52", "rp2040"))
        self.erase_btn.setVisible(t == "nrf52" and bool(d and d.erase))
        self.hint.setText(HINTS.get(t, "") + (" Board nur aus Release-Dateien abgeleitet (*)."
                                               if d and d.extra else ""))
        for wdg in (self.flash_btn, self.file_btn, self.erase_btn, self.sync_btn, self.addon_btn, self.device_cb,
                    self.role_cb, self.version_cb, self.port_cb):
            wdg.setEnabled(not busy)
        self.cancel_btn.setEnabled(busy)
        if not busy:
            fw = d.role(self.role_cb.currentData()) if d else None
            ver = next((v for v in fw.versions if v.version == self.version_cb.currentData()), None) if fw else None
            self.flash_btn.setEnabled(bool(ver) and all(self.lib.have(f) for f in ver.files))

    # --- ports ---

    def refresh_ports(self, force=False):
        if self.worker and not force:
            return  # don't fight the bootloader re-enumeration during a flash
        new = ports.list_serial()
        if not force and [p.device for p in new] == [p.device for p in self.port_list]:
            return
        appeared = [p for p in new if p.device not in {x.device for x in self.port_list}]
        cur = self.port_cb.currentData()
        self.port_list = new
        self.port_cb.blockSignals(True)
        self.port_cb.clear()
        for p in new:
            fam = TYPE_TITLES.get(p.family)
            self.port_cb.addItem(p.label + (f"  ({fam}?)" if fam else ""), p.device)
        self.port_cb.addItem("(kein Port – UF2-Laufwerk / ST-Link)", None)
        self.port_cb.blockSignals(False)
        target = appeared[0].device if appeared and not force else cur
        i = self.port_cb.findData(target)
        self.port_cb.setCurrentIndex(i if i >= 0 else 0)
        self.port_changed(auto=bool(appeared))

    def current_port(self) -> ports.Port | None:
        dev = self.port_cb.currentData()
        return next((p for p in self.port_list if p.device == dev), None)

    def port_changed(self, *_, auto=True):
        p = self.current_port()
        mem = state.recall(p.key) if p else None
        if mem and auto and self.worker is None:
            i = self.device_cb.findData(mem["device"])
            if i >= 0:
                self.device_cb.setCurrentIndex(i)
                j = self.role_cb.findData(mem["role"])
                if j >= 0:
                    self.role_cb.setCurrentIndex(j)
                self.log(f"{p.device}: zuletzt geflasht als {self.device_cb.currentText().split('  ')[0]}"
                         f" / {self.role_cb.currentText()}")

    # --- actions ---

    def log(self, text):
        self.logw.appendPlainText(text)
        self.logw.ensureCursorVisible()

    def start(self, fn, title):
        self.logw.appendPlainText("")
        self.log(f"━━ {title} ━━")
        self.bar.setValue(0)
        self.worker = Worker(fn)
        self.worker.log.connect(self.log)
        self.worker.progress.connect(lambda p: self.bar.setValue(int(p * 10)))
        self.worker.done.connect(self.finished)
        self.update_state()
        self.worker.start()

    def finished(self, ok, err):
        self.worker.wait()
        self.worker = None
        if ok:
            self.bar.setValue(1000)
            self.log("✓ Erfolgreich.")
        else:
            self.log("✗ " + err)
            QMessageBox.warning(self, "Fehlgeschlagen", err)
        self.fill_devices()
        self.refresh_ports(force=True)
        self.update_state()
        self.report_outdated_addons()

    def report_outdated_addons(self):
        for n, have, want in addons.outdated(self.lib):
            self.log(f"🧩 Addon {n}: gebaut für {have or '–'}, neueste Release {want}. "
                     "Über „Addons“ neu bauen.")

    def build_addons(self):
        avail = addons.available()
        if not avail:
            QMessageBox.information(self, "Addons", "Keine Addons gefunden (Ordner: "
                                    + ", ".join(str(d) for d in addon_dirs()) + ").")
            return
        todo = [n for n, _h, _w in addons.outdated(self.lib)]
        lines = []
        for n, ad in avail.items():
            have = addons.built_version(self.lib, ad)
            lines.append(f"• {ad['title']} – {ad.get('description', '')}\n   "
                         + (f"gebaut für {have}" if have else "noch nicht gebaut")
                         + ("  → wird neu gebaut" if n in todo else ""))
        names = todo or list(avail)
        msg = "\n".join(lines) + ("\n\nJetzt bauen?" if todo else "\n\nAlles aktuell. Trotzdem neu bauen?")
        if QMessageBox.question(self, "Addons", msg) != QMessageBox.StandardButton.Yes:
            return

        def job(w):
            for n in names:
                addons.build(self.lib, n, w.flasher.run, w.log.emit)

        self.start(job, "Baue Addons")

    def flash(self):
        d = self.current_device()
        role = self.role_cb.currentData()
        version = self.version_cb.currentData()
        p = self.current_port()
        if not d or not version:
            return
        if d.type in ("esp32",) and not p:
            QMessageBox.information(self, "Port fehlt", "Bitte das Gerät per USB anschließen und den Port wählen.")
            return
        if p and p.family and d.type != p.family and p.family != "esp32":
            if QMessageBox.question(self, "Passt das?", f"Der Port sieht nach {TYPE_TITLES[p.family]} aus, "
                                    f"die Firmware ist für {TYPE_TITLES[d.type]}. Trotzdem flashen?") \
                    != QMessageBox.StandardButton.Yes:
                return
        wipe = self.wipe_chk.isChecked() and d.type == "esp32"
        if wipe and QMessageBox.question(
                self, "Komplett löschen?", "Der gesamte Flash wird gelöscht: Einstellungen, Kontakte, "
                "Kanäle, Identität (Schlüssel) und Bluetooth-Pairings gehen verloren. Weiter?") \
                != QMessageBox.StandardButton.Yes:
            return
        method = self.method_cb.currentData()
        in_boot = self.boot_chk.isChecked()
        port = p.device if p else None
        key = p.key if p else None

        def job(w):
            w.flasher.flash_release(self.lib, d, role, version, port, wipe=wipe, method=method,
                                    in_bootloader=in_boot)
            if key:
                state.remember(key, d.id, role)

        self.start(job, f"Flashe {d.name} · {self.role_cb.currentText()} · {version}")

    def flash_custom(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Firmware-Datei", str(Path.home()), "Firmware (*.bin *.zip *.uf2 *.hex);;Alle Dateien (*)")
        if not path:
            return
        path = Path(path)
        try:
            t = guess_type(path)
        except FlashError as e:
            QMessageBox.warning(self, "Datei", str(e))
            return
        wipe = False
        if t == "esp32" and path.name.endswith("-merged.bin"):
            r = QMessageBox.question(self, "Merged-Image", "Das ist ein komplettes Image (merged). "
                                     "Flash vorher komplett löschen?",
                                     QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
                                     | QMessageBox.StandardButton.Cancel)
            if r == QMessageBox.StandardButton.Cancel:
                return
            wipe = r == QMessageBox.StandardButton.Yes
        p = self.current_port()
        port = p.device if p else None
        in_boot = self.boot_chk.isChecked()
        self.start(lambda w: w.flasher.flash_file(path, port, wipe=wipe, in_bootloader=in_boot),
                   f"Flashe {path.name} ({TYPE_TITLES.get(t, t)})")

    def erase(self):
        d = self.current_device()
        if not d or not d.erase:
            return
        if QMessageBox.question(self, "nRF52 löschen?", "Es wird eine Lösch-Firmware geflasht, die alle "
                                "Einstellungen, Kontakte und die Identität entfernt.\n\nDanach das Gerät per "
                                "Doppelklick auf Reset wieder in den DFU-Modus bringen, „schon im "
                                "Bootloader“ ankreuzen und MeshCore flashen. Weiter?") \
                != QMessageBox.StandardButton.Yes:
            return
        f = self.lib.path(d.erase)
        if not f.exists():
            QMessageBox.warning(self, "Fehlt", f"{d.erase} ist nicht in der Bibliothek.")
            return
        p = self.current_port()
        port = p.device if p else None
        in_boot = self.boot_chk.isChecked()
        self.start(lambda w: w.flasher.flash_file(f, port, dtype="nrf52", in_bootloader=in_boot),
                   f"Lösche {d.name}")

    def cancel(self):
        if self.worker:
            self.worker.cancel()
            self.log("Breche ab …")

    def sync(self):
        ans = QMessageBox.question(
            self, "Neueste Firmware holen",
            "Geräteliste und neueste Firmware aus dem Internet laden?\n\n"
            "„Ja“ lädt die Firmware für alle Geräte (einige hundert MB, bereits vorhandene Dateien "
            "werden übersprungen). „Nein“ nur für das gewählte Gerät.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No | QMessageBox.StandardButton.Cancel)
        if ans == QMessageBox.StandardButton.Cancel:
            return
        d = self.current_device()
        only = None if ans == QMessageBox.StandardButton.Yes or not d else [d.id]

        def job(w):
            try:
                self.lib.sync(devices=only, log=w.log.emit, progress=w.progress.emit,
                              cancelled=lambda: w.cancelled)
            except OSError as e:
                raise FlashError(f"Kein Internet oder Server nicht erreichbar: {e}") from None

        self.start(job, "Synchronisiere Bibliothek")

    def run_doctor(self):
        lines = [("✓ " if ok else "✗ ") + t for ok, t in doctor.checks()]
        present, listed = self.lib.status()
        lines.append(f"{'✓' if listed and present == listed else '✗'} Bibliothek: {present}/{listed} Dateien")
        box = QMessageBox(QMessageBox.Icon.Information, "System prüfen", "\n\n".join(lines), parent=self)
        fix = box.addButton("Jetzt beheben", QMessageBox.ButtonRole.ActionRole) \
            if doctor.udev_fix_needed() else None
        box.addButton(QMessageBox.StandardButton.Close)
        box.exec()
        if fix and box.clickedButton() is fix:
            self.install_udev()

    def offer_udev_fix(self):
        """Asks once per start until the rule is in place; 'Nein' is remembered."""
        if not doctor.udev_fix_needed():
            return
        data = state.load()
        if data.get("udev_declined"):
            self.log("Hinweis: ModemManager kann nRF52-Updates stören. Beheben: 🩺 System prüfen → Jetzt beheben.")
            return
        if QMessageBox.question(
                self, "ModemManager",
                "ModemManager fragt neu angesteckte USB-Geräte als Modem ab. Das stört den Bootloader "
                "von nRF52-Boards (RAK, T-Echo, T114 …) und kann Updates abbrechen lassen.\n\n"
                "Soll der Flasher eine udev-Regel einrichten, damit ModemManager MeshCore-Geräte in "
                "Ruhe lässt? Andere Modems sind nicht betroffen. Dafür ist einmal das Admin-Passwort "
                "nötig.") == QMessageBox.StandardButton.Yes:
            self.install_udev()
        else:
            data["udev_declined"] = True
            state.save(data)
            self.log("Hinweis: udev-Regel nicht eingerichtet. Später: 🩺 System prüfen → Jetzt beheben.")

    def install_udev(self):
        ok, msg = doctor.install_udev_rule()
        self.log(("✓ " if ok else "✗ ") + msg)
        if not ok:
            QMessageBox.warning(self, "udev-Regel", msg + "\n\nAlternativ im Terminal: ./install.sh --udev")

    def closeEvent(self, e):
        if self.worker:
            if QMessageBox.question(self, "Läuft noch", "Es wird gerade geflasht. Wirklich beenden? "
                                    "Das kann das Gerät in einem halbfertigen Zustand lassen.") \
                    != QMessageBox.StandardButton.Yes:
                e.ignore()
                return
            self.worker.cancel()
            self.worker.wait(5000)
        e.accept()


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("MeshCore Flasher")
    app.setDesktopFileName("meshcore-flasher")
    w = Main()
    w.show()
    sys.exit(app.exec())
