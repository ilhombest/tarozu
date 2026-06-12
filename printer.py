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


_PRN_STATUS = {
    0x1: "пауза", 0x2: "ошибка", 0x8: "замятие бумаги", 0x10: "нет бумаги",
    0x40: "проблема с бумагой", 0x80: "принтер офлайн", 0x200: "занят",
    0x400: "печатает", 0x1000: "недоступен", 0x4000: "обработка", 0x10000: "прогрев",
    0x100000: "требуется вмешательство", 0x200000: "нет памяти",
    0x400000: "открыта крышка",
}
_JOB_STATUS = {
    0x1: "пауза", 0x2: "ошибка", 0x4: "удаляется", 0x8: "передаётся",
    0x10: "печатается", 0x20: "принтер офлайн", 0x40: "нет бумаги",
    0x80: "напечатано", 0x100: "удалено", 0x200: "порт не отвечает",
    0x400: "требуется вмешательство", 0x800: "перезапуск",
}


def _decode(flags, table):
    return [name for bit, name in table.items() if flags & bit]


def printer_status(cfg):
    """Опрашивает Windows: состояние принтера и заданий в его очереди -
    чтобы при сбое было видно, на что именно жалуется принтер."""
    if win32print is None:
        return {"ok": False, "error": "статус доступен только на Windows"}
    name = _printer_name(cfg)
    h = win32print.OpenPrinter(name)
    try:
        info = win32print.GetPrinter(h, 2)
        jobs = win32print.EnumJobs(h, 0, 99, 1)
    finally:
        win32print.ClosePrinter(h)
    return {
        "ok": True,
        "printer": name,
        "status": _decode(info["Status"], _PRN_STATUS) or ["готов"],
        "raw_status": info["Status"],
        "jobs": [{
            "id": j["JobId"],
            "doc": j.get("pDocument") or "",
            "status": _decode(j["Status"], _JOB_STATUS) or [f"код {j['Status']}"],
            "raw_status": j["Status"],
        } for j in jobs],
    }


def clear_queue(cfg):
    """Снимает все задания из очереди принтера (зависшие в т.ч.)."""
    if win32print is None:
        raise RuntimeError("доступно только на Windows")
    name = _printer_name(cfg)
    h = win32print.OpenPrinter(name)
    try:
        jobs = win32print.EnumJobs(h, 0, 99, 1)
        for j in jobs:
            try:
                win32print.SetJob(h, j["JobId"], 0, None, 5)  # JOB_CONTROL_DELETE
            except Exception:
                pass
        return len(jobs)
    finally:
        win32print.ClosePrinter(h)


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
        # один шаг этикетки (размер вдоль ленты + зазор) - позиция не уползает
        along = p["label_width_mm"] if int(p.get("rotate", 0)) % 180 == 90 else p["label_height_mm"]
        pitch = int(round((along + p.get("gap_mm", 2)) * 8))
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


def _gdi_dc(printer_name, w_mm=None, h_mm=None):
    """DC принтера; если переданы размеры - страница ровно под этикетку,
    чтобы драйвер не выталкивал пустую этикетку после печати."""
    if w_mm and h_mm:
        try:
            import win32gui
            h = win32print.OpenPrinter(printer_name)
            try:
                dm = win32print.GetPrinter(h, 2)["pDevMode"]
            finally:
                win32print.ClosePrinter(h)
            dm.PaperSize = 256                     # DMPAPER_USER
            dm.PaperWidth = int(round(w_mm * 10))  # десятые доли мм
            dm.PaperLength = int(round(h_mm * 10))
            dm.Fields |= 0x2 | 0x4 | 0x8           # DM_PAPERSIZE|WIDTH|LENGTH
            return win32ui.CreateDCFromHandle(
                win32gui.CreateDC("WINSPOOL", printer_name, dm))
        except Exception:
            pass  # драйвер не принял свой размер - печатаем как есть
    hdc = win32ui.CreateDC()
    hdc.CreatePrinterDC(printer_name)
    return hdc


def _windows_gdi(img, printer_name, off_x_mm=0.0, off_y_mm=0.0, src_dpi=203, page_fit=True):
    page_w = img.width * 25.4 / src_dpi if page_fit else None
    page_h = img.height * 25.4 / src_dpi if page_fit else None
    hdc = _gdi_dc(printer_name, page_w, page_h)
    pw, ph = hdc.GetDeviceCaps(8), hdc.GetDeviceCaps(10)        # HORZRES/VERTRES
    dpix, dpiy = hdc.GetDeviceCaps(88) or 203, hdc.GetDeviceCaps(90) or 203
    # печатаем в истинном размере (мм в мм): пересчёт из DPI рендера в DPI
    # принтера; если этикетка больше листа - ужимаем, но не растягиваем
    k = min(dpix / src_dpi, pw / img.width, ph / img.height)
    w, h = max(1, int(img.width * k)), max(1, int(img.height * k))
    ox = int(round(off_x_mm / 25.4 * dpix))
    oy = int(round(off_y_mm / 25.4 * dpiy))
    hdc.StartDoc("tarozu label")
    hdc.StartPage()
    dib = ImageWin.Dib(img.convert("RGB"))
    dib.draw(hdc.GetHandleOutput(), (ox, oy, ox + w, oy + h))
    hdc.EndPage()
    hdc.EndDoc()
    hdc.DeleteDC()


def print_label(img: Image.Image, cfg) -> str:
    p = cfg["printer"]
    mode = p.get("mode", "escpos")
    rot = int(p.get("rotate", 0)) % 360
    if rot:
        img = img.rotate(-rot, expand=True, fillcolor=1)
    off_x_mm = float(p.get("offset_x_mm", 0))
    off_y_mm = float(p.get("offset_y_mm", 0))
    com_port = (p.get("port") or "").strip()
    # для растровых режимов (8 точек/мм) сдвиг делаем подкладкой белого поля;
    # для драйвера Windows - смещением точки печати (картинка масштабируется)
    if mode != "windows" and (off_x_mm or off_y_mm):
        ox, oy = int(round(off_x_mm * 8)), int(round(off_y_mm * 8))
        canvas = Image.new("1", (img.width + max(0, ox), img.height + max(0, oy)), 1)
        canvas.paste(img.convert("1"), (max(0, ox), max(0, oy)))
        img = canvas
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
        for _ in range(max(1, int(p.get("copies", 1)))):
            _windows_gdi(img, name, off_x_mm, off_y_mm,
                         int(p.get("dpi", 203)), bool(p.get("page_fit", True)))
    else:
        raise ValueError(f"неизвестный режим печати: {mode}")
    return f"отправлено на принтер: {name}"
