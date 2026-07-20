@echo off
chcp 65001 >nul 2>&1
cd /d "%~dp0"
echo Starting QQ AI Bot...
echo Make sure NapCat is running on ws://localhost:3001
echo.
python bot.py
pause