@echo off
chcp 65001 >nul
title Zay_Vietsub.vn - AI Video Auto Translator
echo ===================================================
echo   KHOI DONG ZAY_VIETSUB.VN STUDIO
echo ===================================================
cd /d "%~dp0"
python -m pip install -r requirements.txt --quiet
start "" "http://127.0.0.1:8088"
python -m uvicorn app:app --host 127.0.0.1 --port 8088
pause
