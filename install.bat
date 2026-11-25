@echo off
rem Deminic one-click installer (Windows). Add -Docker to run everything in containers.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
pause
