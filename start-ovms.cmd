@echo off
setlocal
cd /d "%~dp0"
set "ovms_dir=.runtime\ovms-2026.4.1"
if not exist "%ovms_dir%\ovms.exe" (
    echo Run: uv run python -m aivo.setup --ovms
    exit /b 1
)
if not exist "models\aivo-ovms.json" (
    echo Run: uv run python -m aivo.setup --llm
    exit /b 1
)
call "%ovms_dir%\setupvars.bat"
"%ovms_dir%\ovms.exe" --config_path "%CD%\models\aivo-ovms.json" --rest_bind_address 127.0.0.1 --rest_port 8001 --grpc_bind_address 127.0.0.1
exit /b %errorlevel%
