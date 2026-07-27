@echo off
REM Build winMon into a single background .exe (no console window).
REM Run this on a Windows machine with Python 3.10+ installed.

python -m venv venv
call venv\Scripts\activate
pip install --upgrade pip
pip install -r requirements.txt pyinstaller

REM --noconsole = runs silently in the background (no black window).
REM --onefile   = single winMon.exe.
pyinstaller --noconsole --onefile --name winMon ^
  --hidden-import win32gui --hidden-import win32process --hidden-import win32api ^
  --hidden-import win32file --hidden-import win32print --hidden-import winreg ^
  --hidden-import psutil --hidden-import uiautomation ^
  --hidden-import PIL.ImageGrab --hidden-import pynput.keyboard --hidden-import pynput.mouse ^
  agent.py

echo.
echo Built: dist\winMon.exe
