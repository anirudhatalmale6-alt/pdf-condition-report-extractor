@echo off
echo ==========================================
echo   ORBAS - Windows Installer Build Script
echo ==========================================
echo.

REM Check Python
python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python not found. Please install Python 3.10+ from python.org
    pause
    exit /b 1
)

echo Installing dependencies...
pip install PyMuPDF pdfplumber Pillow pytesseract requests pyinstaller

echo.
echo Building ORBAS.exe...
pyinstaller orbas.spec --clean

echo.
if exist "dist\ORBAS.exe" (
    echo BUILD SUCCESSFUL!
    echo Output: dist\ORBAS.exe
) else (
    echo BUILD FAILED - check errors above
)
pause
