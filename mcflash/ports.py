"""Serial port discovery, chip family guess from USB IDs, 1200-baud touch."""

import time
from dataclasses import dataclass

import serial
from serial.tools import list_ports

# USB vendor id -> likely device family
VENDORS = {
    0x303A: ("esp32", "Espressif USB"),
    0x10C4: ("esp32", "CP210x"),
    0x1A86: ("esp32", "CH34x"),
    0x0403: ("esp32", "FTDI"),
    0x239A: ("nrf52", "Adafruit/nRF52"),
    0x2886: ("nrf52", "Seeed"),
    0x1915: ("nrf52", "Nordic"),
    0x2E8A: ("rp2040", "Raspberry Pi"),
    0x0483: ("stm32", "STMicro"),
}


@dataclass
class Port:
    device: str
    vid: int | None
    pid: int | None
    serial_number: str | None
    location: str | None
    product: str

    @property
    def family(self) -> str | None:
        return VENDORS.get(self.vid, (None,))[0]

    @property
    def label(self) -> str:
        ids = f"{self.vid:04x}:{self.pid:04x}" if self.vid is not None else "?"
        chip = VENDORS.get(self.vid, (None, ""))[1]
        desc = self.product or chip
        return f"{self.device}  –  {desc} [{ids}]"

    @property
    def key(self) -> str:
        """Stable identity of the physical device (for remembered choices)."""
        if self.serial_number:
            return f"{self.vid:04x}:{self.pid:04x}:{self.serial_number}"
        return f"loc:{self.location}" if self.location else self.device


def list_serial() -> list[Port]:
    out = []
    for p in list_ports.comports():
        if p.vid is None and not p.device.startswith(("/dev/ttyACM", "/dev/ttyUSB")):
            continue  # built-in ttyS*
        out.append(Port(p.device, p.vid, p.pid, p.serial_number, p.location,
                        p.product or p.description or ""))
    return sorted(out, key=lambda p: p.device)


def touch_1200(device: str, log=print) -> None:
    """Opening the port at 1200 baud makes Arduino-based nRF52/RP2040 firmware
    reboot into its bootloader."""
    log(f"Setze {device} in den Bootloader (1200-Baud-Touch) …")
    try:
        s = serial.Serial()
        s.port = device
        s.baudrate = 1200
        s.dtr = False
        s.open()
        time.sleep(0.2)
        s.dtr = False
        s.close()
    except serial.SerialException as e:
        # the device often resets while we still hold the port
        log(f"   ({e})")


def wait_for_bootloader_port(before: Port, old_names: set[str], timeout=12.0, log=print) -> str:
    """After a touch the bootloader enumerates as a (possibly new) serial port.
    Prefer the same USB location, then any new port, then the old name.
    old_names: port names that existed before the touch."""
    deadline = time.time() + timeout
    gone_seen = False
    time.sleep(0.5)
    while time.time() < deadline:
        ports = list_serial()
        names = {p.device for p in ports}
        if before.device not in names:
            gone_seen = True
        same_loc = [p for p in ports if before.location and p.location == before.location]
        new = [p for p in ports if p.device not in old_names]
        if gone_seen and same_loc:
            return same_loc[0].device
        if new:
            return new[0].device
        if gone_seen and before.device in names:
            return before.device
        time.sleep(0.3)
    if before.device in {p.device for p in list_serial()}:
        log("   Port hat sich nicht neu angemeldet, versuche es trotzdem.")
        return before.device
    raise TimeoutError("Bootloader-Port ist nicht aufgetaucht. Gerät per Doppelklick auf Reset in den "
                       "DFU-Modus bringen und „schon im Bootloader“ ankreuzen.")
