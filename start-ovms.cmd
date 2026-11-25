@echo off
setlocal
cd /d "%~dp0"
set "ovms_dir=.runtime\ovms-2026.4.1"
if not exist "%ovms_dir%\ovms.exe" (
    echo Run: uv run python -m aivo.setup --ovms
    exit /b 1
)
if not exist "models\Qwen3-4B\openvino_model.bin" (
    echo Run: uv run python -m aivo.setup --llm
    exit /b 1
)
call "%ovms_dir%\setupvars.bat"
"%ovms_dir%\ovms.exe" --model_path "%CD%\models\Qwen3-4B" --model_name Qwen3-4B --task text_generation --rest_bind_address 127.0.0.1 --rest_port 8001 --grpc_bind_address 127.0.0.1 --tool_parser hermes3 --reasoning_parser qwen3
exit /b %errorlevel%
