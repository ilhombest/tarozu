# Tarozu — весовой учёт и печать этикеток

Программа для Windows 10: читает вес с электронных весов (COM-порт),
показывает его в браузере и печатает этикетки на термопринтере.
Рассчитана на китайские весовые моноблоки (mini-PC + весовая платформа +
встроенный принтер этикеток), но работает с любыми весами с RS-232/USB-COM
выходом и любым TSPL/ZPL-термопринтером.

## Архитектура

- **Python + stdlib HTTP-сервер** — без тяжёлых фреймворков; UI открывается
  в браузере на `http://127.0.0.1:8077`.
- `scale.py` — фоновый поток чтения веса (pyserial), протоколы CAS / generic /
  auto / demo, автопоиск COM-порта.
- `label.py` + `barcode.py` — этикетка рендерится в картинку (Pillow):
  название, нетто/брутто, производитель, EAN-13 или Code128, даты.
  Кириллица гарантирована на любом принтере.
- `printer.py` — RAW-печать TSPL (BITMAP) или ZPL (^GFA) через pywin32,
  либо печать через драйвер Windows, либо в файл `label.png` (отладка).
- `server.py` / `app.py` — API и запуск; `web/index.html` — интерфейс.

## Быстрый старт (без сборки)

```bat
pip install -r requirements.txt
python app.py
```

Откроется браузер. По умолчанию `scale.protocol = "auto"` и `port = "auto"` —
программа сама найдёт USB-COM порт весов. Для проверки без оборудования
поставьте `"protocol": "demo"` в `config.json`.

## Сборка в один exe

На Windows запустите `build.bat` — получите `dist\tarozu.exe`.
Запуск двойным кликом, установка не нужна. `config.json` и `products.json`
создаются рядом с exe при первом запуске и редактируются блокнотом.

## Настройка (config.json)

| Ключ | Значение |
|---|---|
| `scale.protocol` | `auto` / `cas` / `generic` / `demo` |
| `scale.port` | `auto` или `COM3`, `COM4`… |
| `scale.baudrate` | обычно 9600 (бывает 1200/2400/4800/19200) |
| `scale.poll_command_hex` | команда опроса, если весы не шлют вес сами (напр. `1B70` или `57`) |
| `printer.name` | имя принтера Windows; пусто = принтер по умолчанию |
| `printer.mode` | `tspl` (Xprinter/Gprinter/HPRT и пр.) / `zpl` (Zebra) / `windows` / `file` |
| `printer.label_width_mm/height_mm` | размер этикетки, по умолчанию 58×40 |
| `barcode.type` | `ean13` (весовой: 22 + PLU + граммы + контрольная) или `code128` |
| `company.*` | название, адрес, ИНН, телефон — печатаются на этикетке |

Список товаров — `products.json`: PLU, название, срок годности (дней),
тара упаковки (г).

## Как определить порт и протокол весов

1. Диспетчер устройств → «Порты (COM и LPT)» — найдите USB-Serial (CH340/CP210x).
2. Если вес не появился: проверьте `baudrate` (чаще всего 9600, иногда 1200/2400).
3. Если весы отвечают только по запросу — впишите `poll_command_hex`
   из инструкции к весам.

## API (для интеграции)

- `GET /api/state` — текущий вес `{weight_g, stable, connected, port}`
- `GET /api/products` — список товаров
- `GET /api/preview?plu=1&tare_g=10&prod_date=…&exp_date=…` — PNG этикетки
- `POST /api/print` — печать `{plu, tare_g, prod_date, exp_date}`
- `GET /api/setup` — доступные COM-порты и принтеры
