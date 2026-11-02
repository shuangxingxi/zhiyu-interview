@echo off
chcp 936 >nul
title ZhiYu Share
cd /d "%~dp0"
echo.
echo   1 = 同一 WiFi 局域网分享（同一网络的人都能开）
echo   2 = 公网临时链接（任何人、任何网络都能开）
echo.
set /p choice=请输入 1 或 2 后回车：
if "%choice%"=="2" goto pub
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0分享给别人.ps1" -Mode lan
goto end
:pub
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0分享给别人.ps1" -Mode public
:end
if errorlevel 1 pause
