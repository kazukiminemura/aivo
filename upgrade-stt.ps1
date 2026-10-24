param(
    [ValidateSet('small', 'medium', 'large-v3-turbo')]
    [string]$Model = 'small'
)
$ErrorActionPreference = 'Stop'
Push-Location $PSScriptRoot
try {
    $pythonPath = Join-Path $PSScriptRoot '.venv/Scripts/python.exe'
    if (!(Test-Path -LiteralPath $pythonPath)) { throw 'Run uv sync first.' }
    & $pythonPath -m aivo.setup --model $Model
    if ($LASTEXITCODE -ne 0) { throw 'Model download failed. Existing settings were kept.' }

    $modelPath = "models/whisper-$Model"
    # Test on CPU: leave GPU memory available for the conversation model.
    & $pythonPath -c "import sys, numpy as np; from faster_whisper import WhisperModel; model = WhisperModel(sys.argv[1], device='cpu', compute_type='int8', local_files_only=True); segments, _ = model.transcribe(np.zeros(8000, dtype=np.float32), language='ja', beam_size=3, vad_filter=False); list(segments); print('STT load and inference OK')" $modelPath
    if ($LASTEXITCODE -ne 0) { throw 'Model verification failed. Existing settings were kept.' }

    $envPath = Join-Path $PSScriptRoot '.env'
    $envText = if (Test-Path -LiteralPath $envPath) { [IO.File]::ReadAllText($envPath) } else { '' }
    foreach ($setting in @("AIVO_STT_MODEL=$modelPath", 'AIVO_STT_DEVICE=cpu', 'AIVO_STT_COMPUTE=int8')) {
        $key = $setting.Split('=')[0]
        $pattern = '(?m)^' + [regex]::Escape($key) + '=.*$'
        if ([regex]::IsMatch($envText, $pattern)) {
            $envText = [regex]::Replace($envText, $pattern, $setting)
        } else {
            $envText = $envText.TrimEnd() + "`r`n$setting`r`n"
        }
    }
    $tempPath = Join-Path $PSScriptRoot '.env.stt.tmp'
    [IO.File]::WriteAllText($tempPath, $envText, [Text.UTF8Encoding]::new($false))
    Move-Item -LiteralPath $tempPath -Destination $envPath -Force
    Write-Host "STT changed to $Model (CPU int8). Restart aivo to apply."
} finally {
    Pop-Location
}