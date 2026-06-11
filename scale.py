"""Чтение веса с электронных весов через COM-порт (RS-232 / USB-COM).

Поддерживаемые протоколы:
  cas     - текстовый поток CAS (AD/MW/ER и совместимые китайские весы):
            строки вида "ST,GS,+  1.234kg" или "US,NT,-  0.120kg"
  generic - любой текстовый поток: из строки извлекается первое число
            (подходит для большинства китайских весовых модулей,
            которые непрерывно шлют вес в ASCII)
  auto    - сначала пробует cas, если статус не распознан - generic
  demo    - имитация веса без оборудования (для проверки UI и печати)
"""
import re
import threading
import time

try:
    import serial
    import serial.tools.list_ports
except ImportError:  # допускаем запуск без pyserial в demo-режиме
    serial = None

CAS_RE = re.compile(r"(ST|US|OL)\s*,\s*(GS|NT)\s*,?\s*([+-]?\s*\d+\.?\d*)\s*(kg|g)?", re.I)
NUM_RE = re.compile(r"[+-]?\d+\.\d+|[+-]?\d+")


def list_ports():
    """Список доступных COM-портов с описанием (для настройки в UI)."""
    if serial is None:
        return []
    return [{"port": p.device, "desc": p.description} for p in serial.tools.list_ports.comports()]


class ScaleReader(threading.Thread):
    """Фоновый поток: держит соединение с весами и хранит последний вес."""

    def __init__(self, cfg):
        super().__init__(daemon=True)
        self.cfg = cfg
        self.lock = threading.Lock()
        self.weight_g = 0          # брутто, граммы
        self.stable = False
        self.connected = False
        self.error = ""
        self.port_in_use = ""
        self._stop = threading.Event()

    def snapshot(self):
        with self.lock:
            return {
                "weight_g": self.weight_g,
                "stable": self.stable,
                "connected": self.connected,
                "error": self.error,
                "port": self.port_in_use,
            }

    def stop(self):
        self._stop.set()

    # ------------------------------------------------------------------ run
    def run(self):
        if self.cfg.get("protocol") == "demo":
            self._run_demo()
            return
        while not self._stop.is_set():
            try:
                self._run_serial()
            except Exception as e:
                with self.lock:
                    self.connected = False
                    self.error = str(e)
            time.sleep(2)  # пауза перед переподключением

    def _run_demo(self):
        import math
        t0 = time.time()
        while not self._stop.is_set():
            t = time.time() - t0
            w = 1200 + 350 * math.sin(t / 6)          # «положили товар»
            stable = abs(math.cos(t / 6)) < 0.35
            with self.lock:
                self.weight_g = int(round(w / 2) * 2)
                self.stable = stable
                self.connected = True
                self.error = ""
                self.port_in_use = "DEMO"
            time.sleep(0.2)

    def _pick_port(self):
        port = self.cfg.get("port", "auto")
        if port and port.lower() != "auto":
            return port
        ports = list_ports()
        if not ports:
            raise RuntimeError("COM-порты не найдены")
        # предпочитаем USB-Serial адаптеры (CH340/CP210x/FTDI - типично для китайских весов)
        for p in ports:
            d = p["desc"].lower()
            if any(k in d for k in ("ch340", "cp210", "ftdi", "usb-serial", "usb serial")):
                return p["port"]
        return ports[0]["port"]

    def _run_serial(self):
        if serial is None:
            raise RuntimeError("pyserial не установлен")
        port = self._pick_port()
        poll = bytes.fromhex(self.cfg.get("poll_command_hex", "") or "")
        with serial.Serial(
            port=port,
            baudrate=self.cfg.get("baudrate", 9600),
            bytesize=self.cfg.get("bytesize", 8),
            parity=self.cfg.get("parity", "N"),
            stopbits=self.cfg.get("stopbits", 1),
            timeout=0.5,
        ) as ser:
            with self.lock:
                self.port_in_use = port
            buf = b""
            last_poll = 0.0
            last_data = time.time()
            while not self._stop.is_set():
                if poll and time.time() - last_poll >= self.cfg.get("poll_interval", 0.3):
                    ser.write(poll)
                    last_poll = time.time()
                chunk = ser.read(64)
                if chunk:
                    last_data = time.time()
                    buf += chunk
                    *lines, buf = re.split(rb"[\r\n]+", buf)
                    for line in lines:
                        self._parse_line(line)
                    if len(buf) > 256:  # поток без перевода строки
                        self._parse_line(buf)
                        buf = b""
                elif time.time() - last_data > 5:
                    raise RuntimeError(f"{port}: весы не отвечают")

    def _parse_line(self, raw: bytes):
        try:
            line = raw.decode("ascii", "ignore").strip()
        except Exception:
            return
        if not line:
            return
        proto = self.cfg.get("protocol", "auto")
        parsed = None
        if proto in ("cas", "auto"):
            m = CAS_RE.search(line)
            if m:
                value = float(m.group(3).replace(" ", ""))
                unit = (m.group(4) or "kg").lower()
                grams = value * (1000 if unit == "kg" else 1)
                parsed = (grams, m.group(1).upper() == "ST")
        if parsed is None and proto in ("generic", "auto"):
            m = NUM_RE.search(line)
            if m:
                value = float(m.group(0))
                # эвристика: целые большие числа считаем граммами, дробные - кг
                grams = value * 1000 if ("." in m.group(0) and abs(value) < 200) else value
                parsed = (grams, True)
        if parsed is None:
            return
        grams, stable = parsed
        with self.lock:
            self.weight_g = int(round(grams))
            self.stable = stable
            self.connected = True
            self.error = ""
