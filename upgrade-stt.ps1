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

    $envPath = Join-Path $PSScriptRoot '.env'
    $envText = if (Test-Path -LiteralPath $envPath) { [IO.File]::ReadAllText($envPath) } else { '' }
    foreach ($setting in @("AIVO_STT_MODEL=whisper-$Model")) {
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
    Write-Host "STT changed to $Model (CPU int8). Restart OVMS and aivo to apply."
} finally {
    Pop-Location
}