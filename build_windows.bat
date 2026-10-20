@echo off
setlocal
cd /d "%~dp0"
py -m pip install --upgrade pyinstaller pyserial pillow
if errorlevel 1 goto :fail
py -m PyInstaller --noconfirm --clean --onefile --windowed --name OMG_NTRIP_RTCM_Logger_Bridge --icon omg_icon.ico --add-data "omg_icon.ico;." --collect-submodules serial ntrip_gui.py
if errorlevel 1 goto :fail
echo.
echo Build complete: %~dp0dist\OMG_NTRIP_RTCM_Logger_Bridge.exe
pause
exit /b 0
:fail
echo Build failed. Check the messages above.
pause
exit /b 1
