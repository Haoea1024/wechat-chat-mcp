# Installer orchestration only: mock all executable calls; no client or WeChat access.
$ErrorActionPreference = 'Stop'
$installer = Join-Path $PSScriptRoot '..\install.ps1'
$global:wechatTestCalls = @()
$global:wechatTestFailStage = ''
function global:.venv\Scripts\python.exe {
    $global:wechatTestCalls += ,@('pip')
    $global:LASTEXITCODE = if ($global:wechatTestFailStage -eq 'pip') { 1 } else { 0 }
}
function global:.venv\Scripts\wechat-assistant.exe {
    $global:wechatTestCalls += ,@($args)
    $stage = if ($args[0] -eq '--data-dir') { $args[2] } else { $args[0] }
    $global:LASTEXITCODE = if ($global:wechatTestFailStage -eq $stage) { 1 } else { 0 }
}
# Force the existing-venv branch even when this test runs before dependencies install.
function Test-Path { param($LiteralPath) return $true }
function Assert-True($condition, $message) { if (-not $condition) { throw $message } }
try {
    foreach ($stage in @('pip', 'connect', 'sync')) {
        $global:wechatTestCalls = @()
        $global:wechatTestFailStage = $stage
        $failed = $false
        try { & $installer -Client workbuddy } catch { $failed = $true }
        Assert-True $failed "Expected failure at $stage"
        Assert-True (-not (($global:wechatTestCalls | ForEach-Object { $_ -join '|' }) -match 'register-client')) 'Registered after a failed stage'
    }
    $global:wechatTestFailStage = ''
    $global:wechatTestCalls = @()
    & $installer -Client workbuddy -DataDir 'C:\test data' -DbDir 'C:\test account' -ConfigPath 'C:\test client\mcp.json'
    Assert-True ($global:wechatTestCalls.Count -eq 4) 'Expected pip, connect, sync, register'
    Assert-True (($global:wechatTestCalls[1] -join '|') -eq 'connect|--db-dir|C:\test account') 'Account path forwarding failed'
    Assert-True (($global:wechatTestCalls[3] -join '|') -eq '--data-dir|C:\test data|register-client|workbuddy|--config-path|C:\test client\mcp.json') 'Registration arguments changed'
    $global:wechatTestCalls = @()
    & $installer -Client dsh -ProfileDir 'C:\test profile' -SkipSync
    Assert-True ($global:wechatTestCalls.Count -eq 2) 'SkipSync unexpectedly touched WeChat'
    Assert-True (($global:wechatTestCalls[1] -join '|') -eq 'register-client|dsh|--profile-dir|C:\test profile') 'Profile path forwarding failed'
    Write-Output 'PASS: installer order, failure stops, paths, and registration-only mode'
} finally {
    Remove-Variable wechatTestCalls,wechatTestFailStage -Scope Global -ErrorAction SilentlyContinue
    Remove-Item 'Function:\.venv\Scripts\python.exe' -ErrorAction SilentlyContinue
    Remove-Item 'Function:\.venv\Scripts\wechat-assistant.exe' -ErrorAction SilentlyContinue
}
