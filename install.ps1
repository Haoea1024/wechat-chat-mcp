param(
    [ValidateSet('workbuddy', 'codex', 'dsh')][string]$Client,
    [string]$Python,
    [string]$DbDir,
    [string]$DataDir,
    [string]$ConfigPath,
    [string]$ProfileDir,
    [switch]$SkipSync,
    [switch]$SkipMedia,
    [switch]$KeepModelProvider
)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
. (Join-Path $PSScriptRoot 'scripts\native-command.ps1')
if ([System.Environment]::OSVersion.Platform -ne 'Win32NT') { throw 'This installer supports Windows only.' }
if ($Client -eq 'dsh' -and -not $ProfileDir) {
    throw 'DSH requires -ProfileDir pointing to the active, initialized profile.'
}
if ($Client -in @('workbuddy', 'codex') -and $ProfileDir) { throw 'WorkBuddy/Codex does not use -ProfileDir.' }
if ($Client -eq 'dsh' -and $ConfigPath) { throw 'DSH does not use -ConfigPath.' }
if (-not (Test-Path -LiteralPath '.venv\Scripts\python.exe')) {
    if ($Python) { $pythonCommand = $Python; $pythonPrefix = @() }
    elseif (Get-Command py -ErrorAction SilentlyContinue) { $pythonCommand = 'py'; $pythonPrefix = @('-3') }
    else { $pythonCommand = 'python'; $pythonPrefix = @() }
    Invoke-WeChatNative $pythonCommand ($pythonPrefix + @('-c', 'import sys; assert sys.version_info >= (3,10)')) 'Provide a working Python 3.10+ using -Python.'
    Invoke-WeChatNative $pythonCommand ($pythonPrefix + @('-m', 'venv', '.venv')) 'Could not create .venv.'
}
Invoke-WeChatNative '.venv\Scripts\python.exe' @('-m', 'pip', '--disable-pip-version-check', 'install', '-e', '.[mcp,media]') 'Dependency installation failed; fix the reported error and rerun.'
if (-not $Client) {
    Write-Host 'Dependencies installed. Follow docs/INSTALL.md to connect and register your client.'
    exit 0
}
$commonArgs = @()
if ($DataDir) { $commonArgs += @('--data-dir', $DataDir) }
if ($Client -in @('workbuddy', 'codex') -and -not $KeepModelProvider) {
    Invoke-WeChatNative '.venv\Scripts\wechat-assistant.exe' ($commonArgs + @('use-client')) 'Could not switch to current-client mode.'
}
if (-not $SkipMedia) {
    Invoke-WeChatNative '.venv\Scripts\wechat-assistant.exe' ($commonArgs + @('prepare-media')) 'Speech setup incomplete. Resolve the download/runtime error and rerun. Use -SkipMedia only for an explicitly text-only installation.'
} else { Write-Host 'Media preparation skipped: speech readiness is NOT verified.' }
if (-not $SkipSync) {
    $connectArgs = @('connect')
    if ($DbDir) { $connectArgs += @('--db-dir', $DbDir) }
    Invoke-WeChatNative '.venv\Scripts\wechat-assistant.exe' $connectArgs 'WeChat connection incomplete. Resolve login/account selection and rerun.'
    Invoke-WeChatNative '.venv\Scripts\wechat-assistant.exe' ($commonArgs + @('sync')) 'Local sync failed. Client registration has not run.'
}
$registerArgs = @('register-client', $Client)
if ($ConfigPath) { $registerArgs += @('--config-path', $ConfigPath) }
if ($ProfileDir) { $registerArgs += @('--profile-dir', $ProfileDir) }
Invoke-WeChatNative '.venv\Scripts\wechat-assistant.exe' ($commonArgs + $registerArgs) 'Client registration incomplete. Fix the reported issue and rerun.'
Write-Host 'Step 1 complete: configuration saved and standalone MCP checked.'
Write-Host 'Step 2: verify client trust/enablement. Step 3: call project_status through native tools in the current client. Only step 3 confirms native activation. See docs/INSTALL.md.'
if ($Client -eq 'workbuddy') {
    Write-Host 'WorkBuddy 5.9.3: follow the UI route printed in next_step. Confirm Trust if requested, connect, then call project_status through native tools. Restarting or call-tool cannot replace client approval.'
}
if ($SkipSync) { Write-Host 'WeChat connection and sync were skipped; data readiness has NOT been verified.' }
