"""Печать этикетки.

Режимы (config.json -> printer.mode):
  escpos  - растровая печать ESC/POS (чековые принтеры и встроенные
            термопринтеры китайских POS-моноблоков)
  tspl    - картинка отправляется RAW-командой BITMAP (TSPL/TSPL2: Xprinter,
            Gprinter, HPRT, Atol и почти все китайские термопринтеры)
  zpl     - то же для Zebra-совместимых (команда ^GFA)
  windows - печать через драйвер Windows (GDI), если RAW не подходит
  file    - сохранить label.png рядом с программой (отладка без принтера)
"""
import os
import sys

try:
    import win32print
    import win32ui
    from PIL import ImageWin
except ImportError:
    win32print = None

try:
    import serial
except ImportError:
    serial = None

from PIL import Image


def list_printers():
    if win32print is None:
        return []
    flags = win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS
    return [p[2] for p in win32print.EnumPrinters(flags)]


def _printer_name(cfg):
    name = cfg["printer"].get("name") or ""
    if name:
        return name
    if win32print is None:
        raise RuntimeError("pywin32 недоступен")
    return win32print.GetDefaultPrinter()


def _serial_print(cfg, payload: bytes):
    """Прямая печать на принтер, подключённый через COM-порт (минуя Windows)."""
    if serial is None:
        raise RuntimeError("pyserial не установлен")
    p = cfg["printer"]
    with serial.Serial(port=p["port"], baudrate=int(p.get("baudrate", 9600)),
                       timeout=3, write_timeout=10) as ser:
        ser.write(payload)
        ser.flush()


def _raw_print(printer_name: str, payload: bytes, doc="tarozu label"):
    h = win32print.OpenPrinter(printer_name)
    try:
        win32print.StartDocPrinter(h, 1, (doc, None, "RAW"))
        win32print.StartPagePrinter(h)
        win32print.WritePrinter(h, payload)
        win32print.EndPagePrinter(h)
        win32print.EndDocPrinter(h)
    finally:
        win32print.ClosePrinter(h)


def _img_rows(img: Image.Image):
    """(width_bytes, height, bytes) - по 1 биту на точку, 1=белое."""
    img = img.convert("1")
    w, h = img.size
    wb = (w + 7) // 8
    padded = Image.new("1", (wb * 8, h), 1)
    padded.paste(img, (0, 0))
    return wb, h, padded.tobytes()


def _tspl(img, cfg) -> bytes:
    p = cfg["printer"]
    wb, h, data = _img_rows(img)  # в TSPL BITMAP бит 0 печатается чёрным
    head = (
        f"SIZE {p['label_width_mm']} mm,{p['label_height_mm']} mm\r\n"
        f"GAP {p.get('gap_mm', 2)} mm,0\r\n"
        "DIRECTION 1\r\nCLS\r\n"
    ).encode("ascii")
    bitmap = f"BITMAP 0,0,{wb},{h},0,".encode("ascii") + data + b"\r\n"
    copies = max(1, int(p.get("copies", 1)))
    return head + bitmap + f"PRINT {copies},1\r\n".encode("ascii")


def _zpl(img, cfg) -> bytes:
    wb, h, data = _img_rows(img)
    inverted = bytes(b ^ 0xFF for b in data)  # в ZPL бит 1 - чёрный
    total = wb * h
    hexdata = inverted.hex().upper()
    copies = max(1, int(cfg["printer"].get("copies", 1)))
    return (
        f"^XA^PW{img.width}^LL{img.height}^FO0,0"
        f"^GFA,{total},{total},{wb},{hexdata}^FS^PQ{copies}^XZ"
    ).encode("ascii")


def _escpos(img: Image.Image, cfg) -> bytes:
    p = cfg["printer"]
    width = int(p.get("escpos_width_dots", 384))  # 384 точки = 58-мм чековый
    if img.width > width:
        img = img.resize((width, max(1, round(img.height * width / img.width))), Image.LANCZOS)
    wb, h, data = _img_rows(img)
    raster = bytes(b ^ 0xFF for b in data)  # в ESC/POS бит 1 - чёрный
    if p.get("gap_feed", False):
        # FF: протяжка до следующей этикетки по датчику зазора (если поддерживается)
        tail = b"\x0c"
    else:
        # без датчика: докручиваем ленту так, чтобы печать занимала ровно
        # один шаг этикетки (высота + зазор) - тогда позиция не уползает
        pitch = int(round((p["label_height_mm"] + p.get("gap_mm", 2)) * 8))
        rest = max(0, pitch - h)
        tail = b""
        while rest > 0:
            n = min(255, rest)
            tail += b"\x1bJ" + bytes((n,))  # ESC J n - прогон на n точек
            rest -= n
    one = (
        b"\x1b@"                                   # сброс
        + b"\x1dv0\x00"                            # GS v 0: растровое изображение
        + bytes((wb % 256, wb // 256, h % 256, h // 256))
        + raster
        + tail
        + (b"\x1bi" if p.get("cut", True) else b"")  # ESC i - отрез (если есть резак)
    )
    return one * max(1, int(p.get("copies", 1)))


def _windows_gdi(img, printer_name):
    hdc = win32ui.CreateDC()
    hdc.CreatePrinterDC(printer_name)
    # вписываем в печатную область с сохранением пропорций (8=HORZRES, 10=VERTRES)
    pw, ph = hdc.GetDeviceCaps(8), hdc.GetDeviceCaps(10)
    k = min(pw / img.width, ph / img.height)
    w, h = max(1, int(img.width * k)), max(1, int(img.height * k))
    hdc.StartDoc("tarozu label")
    hdc.StartPage()
    dib = ImageWin.Dib(img.convert("RGB"))
    dib.draw(hdc.GetHandleOutput(), (0, 0, w, h))
    hdc.EndPage()
    hdc.EndDoc()
    hdc.DeleteDC()


def print_label(img: Image.Image, cfg) -> str:
    mode = cfg["printer"].get("mode", "escpos")
    rot = int(cfg["printer"].get("rotate", 0)) % 360
    if rot:
        img = img.rotate(-rot, expand=True, fillcolor=1)
    com_port = (cfg["printer"].get("port") or "").strip()
    if com_port and mode in ("escpos", "tspl", "zpl"):
        payload = {"escpos": _escpos, "tspl": _tspl, "zpl": _zpl}[mode](img, cfg)
        _serial_print(cfg, payload)
        return f"отправлено на принтер ({com_port})"
    if mode == "file" or win32print is None:
        out = os.path.join(os.path.dirname(os.path.abspath(
            sys.executable if getattr(sys, "frozen", False) else __file__)), "label.png")
        img.save(out)
        return f"принтер недоступен, этикетка сохранена: {out}" if mode != "file" else f"сохранено: {out}"
    name = _printer_name(cfg)
    if mode == "escpos":
        _raw_print(name, _escpos(img, cfg))
    elif mode == "tspl":
        _raw_print(name, _tspl(img, cfg))
    elif mode == "zpl":
        _raw_print(name, _zpl(img, cfg))
    elif mode == "windows":
        for _ in range(max(1, int(cfg["printer"].get("copies", 1)))):
            _windows_gdi(img, name)
    else:
        raise ValueError(f"неизвестный режим печати: {mode}")
    return f"отправлено на принтер: {name}"
