@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo ERROR: DPM+ virtual environment is missing: "%CD%\.venv"
    echo Follow the Windows setup instructions in README.md, then run start.bat again.
    pause
    exit /b 1
)
echo Verifying DPM+ v1.0 dependencies...
".venv\Scripts\python.exe" -m tools.release_check --preflight
if errorlevel 1 (
    echo ERROR: DPM+ dependency verification failed.
    echo Recreate .venv using requirements-lock.txt as described in README.md.
    pause
    exit /b 1
)
echo Starting DPM+ Roaming Reconciliation Tool at http://localhost:8501
".venv\Scripts\python.exe" -m streamlit run app.py --server.address localhost --server.port 8501 --browser.gatherUsageStats false
if errorlevel 1 (
    echo ERROR: DPM+ exited with an error. Review the Streamlit message above.
    pause
    exit /b 1
)
endlocal
