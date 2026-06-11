"""Локальный HTTP-сервер: отдаёт UI и API. Без фреймворков - только stdlib."""
import io
import json
import os
import sys
import threading
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import label
import printer
import scale


DEFAULT_CONFIG = {
    "scale": {
        "protocol": "auto",
        "port": "auto",
        "baudrate": 9600,
        "bytesize": 8,
        "parity": "N",
        "stopbits": 1,
        "poll_command_hex": "",
        "poll_interval": 0.3,
    },
    "printer": {
        "name": "",
        "port": "",
        "baudrate": 9600,
        "cut": True,
        "gap_feed": False,
        "rotate": 0,
        "mode": "escpos",
        "escpos_width_dots": 384,
        "dpi": 203,
        "label_width_mm": 58,
        "label_height_mm": 40,
        "gap_mm": 2,
        "copies": 1,
    },
    "barcode": {"type": "ean13", "prefix": "22"},
    "company": {
        "name": "ООО \"Птицефабрика\"",
        "address": "г. Ташкент, ул. Примерная, 1",
        "inn": "ИНН 123456789",
        "phone": "+998 90 000-00-00",
    },
    "server": {"host": "127.0.0.1", "port": 8077, "open_browser": True},
}

DEFAULT_PRODUCTS = [
    {"plu": 1, "name": "Филе куриное", "shelf_days": 5, "tare_g": 10},
    {"plu": 2, "name": "Голень с кожей", "shelf_days": 5, "tare_g": 10},
    {"plu": 3, "name": "Крылышки", "shelf_days": 5, "tare_g": 10},
    {"plu": 4, "name": "Тушка цыпленка", "shelf_days": 7, "tare_g": 15},
    {"plu": 5, "name": "Бедро куриное", "shelf_days": 5, "tare_g": 10},
    {"plu": 6, "name": "Грудка на кости", "shelf_days": 5, "tare_g": 10},
    {"plu": 7, "name": "Фарш куриный", "shelf_days": 3, "tare_g": 12},
    {"plu": 8, "name": "Печень куриная", "shelf_days": 3, "tare_g": 12},
    {"plu": 9, "name": "Желудки куриные", "shelf_days": 3, "tare_g": 12},
    {"plu": 10, "name": "Сердечки куриные", "shelf_days": 3, "tare_g": 12},
]


def _merge_defaults(cfg, defaults):
    """Дополняет конфиг недостающими ключами из значений по умолчанию."""
    out = dict(defaults)
    for k, v in (cfg or {}).items():
        if isinstance(v, dict) and isinstance(defaults.get(k), dict):
            out[k] = _merge_defaults(v, defaults[k])
        else:
            out[k] = v
    return out


def base_dir():
    """Папка с exe (PyInstaller) или с исходниками."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def resource_dir():
    """Файлы, упакованные внутрь exe (web/)."""
    return getattr(sys, "_MEIPASS", base_dir())


def save_json(name, obj):
    path = os.path.join(base_dir(), name)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def load_json(name, default):
    """Читает файл; если его нет или он битый - создаёт со значениями по умолчанию."""
    path = os.path.join(base_dir(), name)
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        save_json(name, default)
        return default


class App:
    def __init__(self):
        self.cfg = _merge_defaults(load_json("config.json", DEFAULT_CONFIG), DEFAULT_CONFIG)
        self.products = load_json("products.json", DEFAULT_PRODUCTS)
        self.reader = scale.ScaleReader(self._scale_cfg(self.cfg))
        self.reader.start()
        self.print_lock = threading.Lock()

    @staticmethod
    def _scale_cfg(cfg):
        # порт принтера исключаем из автопоиска весов, чтобы их не перепутать
        sc = dict(cfg.get("scale", {}))
        sc["exclude_port"] = cfg.get("printer", {}).get("port", "")
        return sc

    def save_config(self, new_cfg):
        """Сохраняет настройки; при смене параметров весов перезапускает чтение."""
        new_cfg = _merge_defaults(new_cfg, DEFAULT_CONFIG)
        scale_changed = self._scale_cfg(new_cfg) != self._scale_cfg(self.cfg)
        self.cfg = new_cfg
        save_json("config.json", new_cfg)
        if scale_changed:
            self.reader.stop()
            self.reader = scale.ScaleReader(self._scale_cfg(new_cfg))
            self.reader.start()

    def do_testprint(self):
        """Печатает рамку по размеру этикетки - для калибровки положения."""
        from PIL import Image, ImageDraw
        p = self.cfg["printer"]
        w, h = int(p["label_width_mm"] * 8), int(p["label_height_mm"] * 8)
        img = Image.new("1", (w, h), 1)
        d = ImageDraw.Draw(img)
        d.rectangle([0, 0, w - 1, h - 1], outline=0, width=4)
        d.line([0, 0, w - 1, h - 1], fill=0, width=2)
        d.line([w - 1, 0, 0, h - 1], fill=0, width=2)
        with self.print_lock:
            msg = printer.print_label(img, self.cfg)
        return {"ok": True, "message": msg}

    def save_products(self, products):
        cleaned = []
        for p in products:
            name = str(p.get("name", "")).strip()
            if not name:
                continue
            cleaned.append({
                "plu": int(p.get("plu", 0)) or len(cleaned) + 1,
                "name": name,
                "shelf_days": max(1, int(p.get("shelf_days", 5))),
                "tare_g": max(0, int(p.get("tare_g", 0))),
            })
        if not cleaned:
            raise ValueError("список товаров пуст")
        self.products = cleaned
        save_json("products.json", cleaned)

    # ------------------------------------------------------------- печать
    def build_label_data(self, req):
        plu = int(req["plu"])
        prod = next((p for p in self.products if p["plu"] == plu), None)
        if prod is None:
            raise ValueError(f"товар PLU {plu} не найден")
        st = self.reader.snapshot()
        gross = int(req.get("gross_g") or st["weight_g"])
        if gross <= 0:
            raise ValueError("нет веса на весах")
        tare = int(req.get("tare_g", prod.get("tare_g", 0)))
        net = max(0, gross - tare)
        prod_date = req.get("prod_date") or date.today().isoformat()
        pd = datetime.strptime(prod_date, "%Y-%m-%d").date()
        exp_date = req.get("exp_date") or (pd + timedelta(days=prod.get("shelf_days", 5))).isoformat()
        ed = datetime.strptime(exp_date, "%Y-%m-%d").date()
        return {
            "name": prod["name"],
            "net_g": net,
            "gross_g": gross,
            "prod_date": pd.strftime("%d.%m.%Y"),
            "exp_date": ed.strftime("%d.%m.%Y"),
            "barcode_value": label.make_barcode_value(self.cfg, plu, net),
        }

    def do_print(self, req):
        data = self.build_label_data(req)
        img = label.render_label(self.cfg, data)
        with self.print_lock:
            msg = printer.print_label(img, self.cfg)
        return {"ok": True, "message": msg, "label": data}


def make_handler(app: App):
    web_root = os.path.join(resource_dir(), "web")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):  # не засоряем консоль
            pass

        def _json(self, obj, code=200):
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_body(self):
            n = int(self.headers.get("Content-Length", 0))
            return json.loads(self.rfile.read(n) or b"{}")

        def do_GET(self):
            path = self.path.split("?")[0]
            if path == "/api/state":
                st = app.reader.snapshot()
                st["weight_kg"] = f"{st['weight_g'] / 1000:.3f}"
                self._json(st)
            elif path == "/api/products":
                self._json(app.products)
            elif path == "/api/setup":
                self._json({
                    "ports": scale.list_ports(),
                    "printers": printer.list_printers(),
                    "config": app.cfg,
                })
            elif path.startswith("/api/preview"):
                self._preview()
            elif path in ("/", "/index.html"):
                self._file(os.path.join(web_root, "index.html"), "text/html; charset=utf-8")
            else:
                self.send_error(404)

        def _preview(self):
            from urllib.parse import parse_qs, urlparse
            q = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
            try:
                data = app.build_label_data(q)
                img = label.render_label(app.cfg, data)
                buf = io.BytesIO()
                img.convert("L").save(buf, "PNG")
                body = buf.getvalue()
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except Exception as e:
                self._json({"ok": False, "error": str(e)}, 400)

        def do_POST(self):
            try:
                if self.path == "/api/print":
                    self._json(app.do_print(self._read_body()))
                elif self.path == "/api/config":
                    app.save_config(self._read_body())
                    self._json({"ok": True})
                elif self.path == "/api/products":
                    app.save_products(self._read_body())
                    self._json({"ok": True})
                elif self.path == "/api/testprint":
                    self._json(app.do_testprint())
                else:
                    self.send_error(404)
            except Exception as e:
                self._json({"ok": False, "error": str(e)}, 400)

        def _file(self, path, ctype):
            try:
                with open(path, "rb") as f:
                    body = f.read()
            except OSError:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return Handler


def run():
    app = App()
    srv_cfg = app.cfg.get("server", {})
    host = srv_cfg.get("host", "127.0.0.1")
    port = srv_cfg.get("port", 8077)
    httpd = ThreadingHTTPServer((host, port), make_handler(app))
    url = f"http://{host}:{port}/"
    print(f"Tarozu запущен: {url}  (Ctrl+C - выход)")
    if srv_cfg.get("open_browser", True):
        import webbrowser
        threading.Timer(0.5, webbrowser.open, args=(url,)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        app.reader.stop()
