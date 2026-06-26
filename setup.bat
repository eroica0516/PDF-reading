@echo off
:: ============================================================
:: setup.bat  —  PDF Annual Impact Cost Extractor
:: ============================================================
:: Sets up the Python virtual environment, installs dependencies,
:: and runs the extractor against all PDFs in the Input/ folder.
::
:: Requirements: Python 3.10+ must be on your PATH.
:: ============================================================

echo.
echo ============================================================
echo  PDF Annual Impact Cost Extractor
echo ============================================================
echo.

:: Check Python is available
python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python was not found on your PATH.
    echo Please install Python 3.10+ and try again.
    pause
    exit /b 1
)

:: Create virtual environment if it doesn't already exist
if not exist "venv\Scripts\activate.bat" (
    echo Creating virtual environment...
    python -m venv venv
    if errorlevel 1 (
        echo ERROR: Failed to create virtual environment.
        pause
        exit /b 1
    )
    echo Virtual environment created.
) else (
    echo Virtual environment already exists, skipping creation.
)

echo.
echo Activating virtual environment...
call venv\Scripts\activate.bat

echo.
echo Installing / updating dependencies...
pip install -r requirements.txt --quiet
if errorlevel 1 (
    echo ERROR: Failed to install dependencies.
    pause
    exit /b 1
)
echo Dependencies ready.

echo.
echo Running extractor...
echo --------------------------------------------------------
python extractor.py
echo --------------------------------------------------------

echo.
if exist "Output\Output.csv" (
    echo SUCCESS: Output written to Output\Output.csv
) else (
    echo WARNING: Output\Output.csv was not created.
    echo Check extractor.log for details.
)

echo.
pause
