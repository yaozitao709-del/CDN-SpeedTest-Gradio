@echo off
chcp 65001 >nul
cd /d "%~dp0"
py -3.11 -c "import sys; sys.exit(sys.version_info.major != 3 or sys.version_info.minor not in range(11,30))" >nul 2>nul
if not errorlevel 1 goto python311
py -3 -c "import sys; sys.exit(sys.version_info.major != 3 or sys.version_info.minor not in range(11,30))" >nul 2>nul
if not errorlevel 1 goto pydefault
python -c "import sys; sys.exit(sys.version_info.major != 3 or sys.version_info.minor not in range(11,30))" >nul 2>nul
if not errorlevel 1 goto pythondefault
echo 请先安装 Python 3.11 或更新版本，并勾选 Add Python to PATH。
echo 下载地址：https://www.python.org/downloads/
pause
exit /b 1

:python311
py -3.11 launcher.py %*
goto finished
:pydefault
py -3 launcher.py %*
goto finished
:pythondefault
python launcher.py %*
:finished
set "CDN_APP_EXIT=%ERRORLEVEL%"
if not "%CDN_APP_EXIT%"=="0" pause
exit /b %CDN_APP_EXIT%
