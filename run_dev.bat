@echo off
echo ============================================
echo  MedAiCarePlus — Development Server
echo ============================================

cd /d "c:\Users\ferdinan\Downloads\project_AI_\MedAiCarePlus"

REM Check Ollama
curl -s http://localhost:11434/api/tags >nul 2>&1
IF ERRORLEVEL 1 (
    echo [WARN] Ollama is not running. OCR will be unavailable.
    echo        Start it with:  ollama serve
    echo.
) ELSE (
    echo [OK] Ollama is running.
)

REM Start FastAPI
echo [INFO] Starting FastAPI on http://127.0.0.1:8000
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
