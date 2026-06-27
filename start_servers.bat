@echo off
echo ==========================================
echo   Bloodline Alpha (JRA) - Launcher
echo   (Production build - for long-running use)
echo ==========================================

echo [1/4] Stopping Python processes...
taskkill /F /IM python.exe /T 2>nul
if %errorlevel% == 0 (
    echo   - Python processes terminated.
) else (
    echo   - No Python processes found.
)

echo [2/4] Stopping Node.js processes...
taskkill /F /IM node.exe /T 2>nul
if %errorlevel% == 0 (
    echo   - Node.js processes terminated.
) else (
    echo   - No Node.js processes found.
)

echo [3/4] Waiting for ports to be released...
timeout /t 3 >nul

echo.
echo ==========================================
echo   Starting services...
echo ==========================================
echo   NOTE: backend reads bloodline.db
echo         Make sure JV-Data import is done.
echo ==========================================

echo Starting Backend Server... (FastAPI / Port 8001)
start "Backend Server (Port 8001)" cmd /k "cd /d "%~dp0backend" && "%~dp0venv\Scripts\python.exe" -m app.main"

echo Building Frontend (production)... this may take a while
cd /d "%~dp0frontend"
call npm run build
if %errorlevel% neq 0 (
    echo   - BUILD FAILED. Aborting. Fix the build error and retry.
    cd /d "%~dp0"
    pause
    exit /b 1
)
echo Starting Frontend (production, Port 3000)...
start "Frontend (Port 3000)" cmd /k "cd /d "%~dp0frontend" && npm run start"
cd /d "%~dp0"

echo [4/4] Waiting for servers to initialize...
timeout /t 5 >nul
start http://localhost:3000

echo.
echo ==========================================
echo   Started (PRODUCTION build) - the single launcher for normal use.
echo   - Backend:  http://127.0.0.1:8001
echo   - Frontend: http://localhost:3000 (production, rebuilt each launch)
echo   For frontend dev with HMR, run manually:
echo     cd backend ^&^& python -m app.main
echo     cd frontend ^&^& npm run dev
echo ==========================================
pause
