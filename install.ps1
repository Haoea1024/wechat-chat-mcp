param(
    [ValidateSet('workbuddy', 'dsh')][string]$Client,
    [string]$Python,
    [string]$DbDir,
    [string]$DataDir,
    [string]$ConfigPath,
    [string]$ProfileDir,
    [switch]$SkipSync
)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
if ([System.Environment]::OSVersion.Platform -ne 'Win32NT') { throw 'This installer supports Windows only.' }
if ($Client -eq 'dsh' -and -not $ProfileDir) {
    throw 'DSH requires -ProfileDir pointing to the active, initialized profile.'
}
if ($Client -eq 'workbuddy' -and $ProfileDir) { throw 'WorkBuddy does not use -ProfileDir.' }
if ($Client -eq 'dsh' -and $ConfigPath) { throw 'DSH does not use -ConfigPath.' }
if (-not (Test-Path -LiteralPath '.venv\Scripts\python.exe')) {
    if ($Python) { $pythonCommand = $Python; $pythonPrefix = @() }
    elseif (Get-Command py -ErrorAction SilentlyContinue) { $pythonCommand = 'py'; $pythonPrefix = @('-3') }
    else { $pythonCommand = 'python'; $pythonPrefix = @() }
    & $pythonCommand @pythonPrefix -c 'import sys; assert sys.version_info >= (3,10)'
    if ($LASTEXITCODE -ne 0) { throw 'Provide a working Python 3.10+ using -Python.' }
    & $pythonCommand @pythonPrefix -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw 'Could not create .venv.' }
}
& '.venv\Scripts\python.exe' -m pip install -e '.[mcp,media]'
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed; fix the reported error and rerun.' }
if (-not $Client) {
    Write-Host 'Dependencies installed. Follow docs/INSTALL.md to connect and register your client.'
    exit 0
}
$commonArgs = @()
if ($DataDir) { $commonArgs += @('--data-dir', $DataDir) }
if (-not $SkipSync) {
    $connectArgs = @('connect')
    if ($DbDir) { $connectArgs += @('--db-dir', $DbDir) }
    & '.venv\Scripts\wechat-assistant.exe' @connectArgs
    if ($LASTEXITCODE -ne 0) { throw 'WeChat connection incomplete. Resolve login/account selection and rerun.' }
    & '.venv\Scripts\wechat-assistant.exe' @commonArgs sync
    if ($LASTEXITCODE -ne 0) { throw 'Local sync failed. Client registration has not run.' }
}
$registerArgs = @('register-client', $Client)
if ($ConfigPath) { $registerArgs += @('--config-path', $ConfigPath) }
if ($ProfileDir) { $registerArgs += @('--profile-dir', $ProfileDir) }
& '.venv\Scripts\wechat-assistant.exe' @commonArgs @registerArgs
if ($LASTEXITCODE -ne 0) { throw 'Client registration incomplete. Fix the reported issue and rerun.' }
Write-Host 'Registered. Reload MCP in your client and call project_status to finish verification.'
if ($SkipSync) { Write-Host 'WeChat connection and sync were skipped; data readiness has NOT been verified.' }
