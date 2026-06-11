@echo off
rem Сборка Tarozu в один exe (запускать на Windows c установленным Python 3.10+)
chcp 65001 >nul
pip install -r requirements.txt pyinstaller || exit /b 1
pyinstaller --noconfirm --onefile --name tarozu ^
  --add-data "web;web" ^
  --hidden-import win32timezone ^
  app.py
echo.
echo Готово: dist\tarozu.exe
echo Рядом с exe положите config.json и products.json (создадутся сами при первом запуске).
pause
