@echo off
chcp 65001 >nul
set PYTHONUTF8=1
cd /d D:\ai\mt
D:\Python\Python313\python.exe local-mt-gateway.py
echo.
echo [gateway exited] press any key to close
pause >nul