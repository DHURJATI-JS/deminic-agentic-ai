@echo off
rem Deminic one-click runner (Windows).
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0run.ps1" %*
