"""Рендеринг этикетки в чёрно-белую картинку (PIL) по макету из ТЗ:

  +--------------------------------------+
  |        НАЗВАНИЕ ПРОДУКТА (крупно)    |
  |--------------------------------------|
  | НЕТТО:  1.234 кг |  Производитель    |
  | БРУТТО: 1.244 кг |  адрес, ИНН, тел. |
  |--------------------------------------|
  |        ||||| штрихкод |||||          |
  |          2200001012349               |
  |--------------------------------------|
  | Изгот.: 11.06.2026  Годен до: 16.06  |
  +--------------------------------------+

Картинка печатается на термопринтере (TSPL BITMAP) или через драйвер Windows,
поэтому кириллица выводится гарантированно любым принтером.
"""
import os
from PIL import Image, ImageDraw, ImageFont

from barcode import ean13_modules, code128_modules

_FONT_FILES = {
    # (полужирный, курсив) -> кандидаты по порядку
    (False, False): ["arial.ttf", "tahoma.ttf", "DejaVuSans.ttf"],
    (True, False): ["arialbd.ttf", "tahomabd.ttf", "DejaVuSans-Bold.ttf"],
    (False, True): ["ariali.ttf", "DejaVuSans-Oblique.ttf"],
    (True, True): ["arialbi.ttf", "DejaVuSans-BoldOblique.ttf"],
}
_FONT_DIRS = [r"C:\Windows\Fonts", "/usr/share/fonts/truetype/dejavu"]


def _font(size: int, bold=True, italic=False):
    names = _FONT_FILES[(bool(bold), bool(italic))] + _FONT_FILES[(True, False)] + _FONT_FILES[(False, False)]
    for name in names:
        for d in _FONT_DIRS:
            path = os.path.join(d, name)
            if os.path.exists(path):
                try:
                    return ImageFont.truetype(path, size)
                except Exception:
                    pass
    return ImageFont.load_default()


def _fit_text(draw, text, max_w, start_size, min_size=14, bold=True, italic=False):
    """Подбор размера шрифта, чтобы текст влез по ширине."""
    size = start_size
    while size > min_size:
        f = _font(size, bold, italic)
        if draw.textlength(text, font=f) <= max_w:
            return f
        size -= 2
    return _font(min_size, bold, italic)


def make_barcode_value(cfg, plu: int, net_g: int):
    """Весовой EAN-13: префикс(2) + PLU(5) + вес в граммах(5) + контрольная."""
    btype = cfg["barcode"].get("type", "ean13")
    if btype == "ean13":
        prefix = (cfg["barcode"].get("prefix") or "22")[:2].rjust(2, "2")
        body = f"{prefix}{plu:05d}{min(net_g, 99999):05d}"
        full, _ = ean13_modules(body)
        return full
    return f"{plu:04d}-{net_g:05d}"  # code128


def _draw_barcode(img, draw, value, btype, x0, x1, y0, y1, font):
    if btype == "ean13":
        _, bits = ean13_modules(value)
    else:
        bits = code128_modules(value)
    text_h = font.size + 4
    bar_h = (y1 - y0) - text_h
    module = max(1, (x1 - x0) // len(bits))
    bx = (x0 + x1 - module * len(bits)) // 2
    for i, b in enumerate(bits):
        if b == "1":
            draw.rectangle([bx + i * module, y0, bx + (i + 1) * module - 1, y0 + bar_h], fill=0)
    tw = draw.textlength(value, font=font)
    draw.text(((x0 + x1 - tw) // 2, y0 + bar_h + 2), value, font=font, fill=0)


class _SafeDict(dict):
    def __missing__(self, key):
        return "{" + key + "}"


def _tpl_values(cfg, data):
    comp = cfg.get("company", {})
    return _SafeDict(
        name=data["name"],
        net_kg=f"{data['net_g'] / 1000:.3f}",
        gross_kg=f"{data['gross_g'] / 1000:.3f}",
        net_g=str(data["net_g"]),
        gross_g=str(data["gross_g"]),
        prod_date=data["prod_date"],
        exp_date=data["exp_date"],
        barcode=data["barcode_value"],
        company_name=comp.get("name", ""),
        company_address=comp.get("address", ""),
        company_inn=comp.get("inn", ""),
        company_phone=comp.get("phone", ""),
    )


def render_template(cfg, data) -> Image.Image:
    """Рендер по пользовательскому шаблону (редактор дизайна этикетки)."""
    p = cfg["printer"]
    dots_mm = p.get("dpi", 203) / 25.4
    W = int(p.get("label_width_mm", 58) * dots_mm)
    H = int(p.get("label_height_mm", 40) * dots_mm)
    fs = max(0.3, min(3.0, float(p.get("font_scale", 100)) / 100))
    img = Image.new("1", (W, H), 1)
    d = ImageDraw.Draw(img)
    vals = _tpl_values(cfg, data)
    for el in cfg.get("label_template", []):
        try:
            x = int(float(el.get("x_mm", 0)) * dots_mm)
            y = int(float(el.get("y_mm", 0)) * dots_mm)
            w = int(float(el.get("w_mm", 0)) * dots_mm)
            size = float(el.get("size", 14))
            kind = el.get("type", "text")
            if kind == "line":
                d.rectangle([x, y, x + max(1, w), y + max(1, int(size))], fill=0)
            elif kind == "barcode":
                bh = int(size * dots_mm)  # для штрихкода size - высота в мм
                f_bc = _font(max(10, int(12 * fs)), bold=False)
                _draw_barcode(img, d, data["barcode_value"],
                              cfg["barcode"].get("type", "ean13"),
                              x, x + max(40, w), y, y + max(24, bh), f_bc)
            else:  # text
                s = str(el.get("text", "")).format_map(vals)
                if not s.strip():
                    continue
                f = _font(max(7, int(size * fs)), el.get("bold", False), el.get("italic", False))
                if w > 0 and d.textlength(s, font=f) > w:  # ужимаем по ширине
                    f = _fit_text(d, s, w, f.size, min_size=7,
                                  bold=el.get("bold", False), italic=el.get("italic", False))
                tw = d.textlength(s, font=f)
                align = el.get("align", "left")
                tx = x + (max(0, w - tw) // 2 if align == "center"
                          else max(0, w - tw) if align == "right" else 0)
                d.text((tx, y), s, font=f, fill=0)
        except Exception:
            continue  # битый элемент шаблона не валит печать
    return img


def render_label(cfg, data) -> Image.Image:
    """data: dict(name, net_g, gross_g, prod_date, exp_date, barcode_value)."""
    if cfg.get("label_template"):
        return render_template(cfg, data)
    p = cfg["printer"]
    dots_mm = p.get("dpi", 203) / 25.4
    W = int(p.get("label_width_mm", 58) * dots_mm)
    H = int(p.get("label_height_mm", 40) * dots_mm)
    fs = max(0.3, min(3.0, float(p.get("font_scale", 100)) / 100))  # масштаб шрифта
    img = Image.new("1", (W, H), 1)
    d = ImageDraw.Draw(img)
    pad = int(1.5 * dots_mm)

    # --- верх: название продукта
    name = data["name"]
    f_name = _fit_text(d, name, W - 2 * pad, int(H * 0.14 * fs), min_size=max(8, int(14 * fs)))
    tw = d.textlength(name, font=f_name)
    y = pad
    d.text(((W - tw) // 2, y), name, font=f_name, fill=0)
    y += f_name.size + pad // 2
    d.line([pad, y, W - pad, y], fill=0, width=2)
    y += 4

    # --- средний блок: слева вес, справа производитель
    mid_h = int(H * 0.27)
    split = int(W * 0.46)
    net = data["net_g"] / 1000
    gross = data["gross_g"] / 1000
    line_net = f"НЕТТО: {net:.3f} кг"
    line_gross = f"БРУТТО: {gross:.3f} кг"
    f_w = _fit_text(d, line_gross, split - pad - 4, int(mid_h * 0.34 * fs), min_size=max(8, int(14 * fs)))
    d.text((pad, y + 2), line_net, font=f_w, fill=0)
    d.text((pad, y + 2 + int(mid_h * 0.45)), line_gross, font=f_w, fill=0)
    d.line([split, y, split, y + mid_h], fill=0, width=1)

    comp = cfg["company"]
    f_c = _font(max(8, int(mid_h * 0.20 * fs)), bold=False)
    cy = y
    for line in (comp.get("name", ""), comp.get("address", ""),
                 comp.get("inn", ""), comp.get("phone", "")):
        if not line:
            continue
        f_line = _fit_text(d, line, W - split - 2 * pad, f_c.size, max(7, int(10 * fs)), bold=False)
        d.text((split + pad // 2, cy), line, font=f_line, fill=0)
        cy += f_line.size + 2
    y += mid_h
    d.line([pad, y, W - pad, y], fill=0, width=2)

    # --- штрихкод
    bc_h = int(H * 0.30)
    f_bc = _font(max(10, int(bc_h * 0.22 * fs)), bold=False)
    _draw_barcode(img, d, data["barcode_value"], cfg["barcode"].get("type", "ean13"),
                  pad, W - pad, y + 4, y + bc_h, f_bc)
    y += bc_h + 6
    d.line([pad, y, W - pad, y], fill=0, width=2)

    # --- низ: даты
    f_d = _font(max(9, int(H * 0.065 * fs)))
    line = f"Изгот.: {data['prod_date']}   Годен до: {data['exp_date']}"
    f_d = _fit_text(d, line, W - 2 * pad, f_d.size, min_size=max(8, int(14 * fs)))
    d.text((pad, y + 4), line, font=f_d, fill=0)
    return img
