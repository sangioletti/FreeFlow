@echo off
REM ----------------------------------------------------------------------
REM build_windows.bat — produce dist\FreeFlow.exe locally on Windows.
REM
REM Use this if the GitHub Actions CI build isn't available or hasn't
REM produced an artifact yet.  Requires Python (>= 3.9) to already be
REM installed on this machine.
REM
REM From a Command Prompt or PowerShell window in this folder, run:
REM     build_windows.bat
REM Result:  dist\FreeFlow.exe   (a single standalone file)
REM ----------------------------------------------------------------------

setlocal

echo === FreeFlow Windows build ===

REM 1. Ensure pip is current and the runtime + build deps are installed.
python -m pip install --upgrade pip
if errorlevel 1 goto :error

pip install -r requirements.txt
if errorlevel 1 goto :error

pip install PyQt5 pyinstaller
if errorlevel 1 goto :error

REM 2. Build using the project's PyInstaller spec.
pyinstaller --clean --noconfirm freeflow.spec
if errorlevel 1 goto :error

REM 3. Sanity check that the exe was produced.
if not exist dist\FreeFlow.exe (
    echo *** Build appeared to succeed but dist\FreeFlow.exe is missing.
    echo *** Check the PyInstaller log above for errors.
    exit /b 1
)

echo.
echo === Done — dist\FreeFlow.exe is ready. ===
echo You can copy it anywhere and double-click to launch.
exit /b 0

:error
echo *** Build failed.  See the error message above.
exit /b 1
