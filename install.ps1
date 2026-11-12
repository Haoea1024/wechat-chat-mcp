$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
if (-not (Test-Path -LiteralPath '.venv\Scripts\python.exe')) {
    python -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw 'Python 3.10+ is required.' }
}
& '.venv\Scripts\python.exe' -m pip install -e '.[mcp,media]'
if ($LASTEXITCODE -ne 0) { throw 'Installation failed.' }
Write-Host 'Installed. Run .venv\Scripts\wechat-assistant.exe connect, then double-click start.cmd.'
