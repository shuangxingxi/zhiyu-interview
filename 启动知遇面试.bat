@echo off
chcp 65001 >nul
title ZhiYu AI Interview
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0launcher.ps1"
if errorlevel 1 pause
