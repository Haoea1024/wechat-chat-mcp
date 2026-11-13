$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot '..\scripts\native-command.ps1')
$pythonExe = (Resolve-Path (Join-Path $PSScriptRoot '..\.venv\Scripts\python.exe')).Path
Invoke-WeChatNative $pythonExe @('-c', 'import sys; sys.stderr.write(str(123)); sys.exit(0)') 'Unexpected failure on stderr'
if ($ErrorActionPreference -ne 'Stop') { throw 'Preference was not restored' }
$failed = $false
try {
    Invoke-WeChatNative $pythonExe @('-c', 'import sys; sys.stderr.write(str(456)); sys.exit(7)') 'Expected nonzero'
} catch {
    if ($_.Exception.Message -notmatch 'exit 7') { throw }
    $failed = $true
}
if (-not $failed) { throw 'Nonzero exit was swallowed' }
$failed = $false
try { Invoke-WeChatNative 'wechat-nonexistent-test-command' @() 'Missing executable' } catch { $failed = $true }
if (-not $failed) { throw 'Missing executable was treated as success' }
Write-Output 'PASS: stderr with zero exit, nonzero exit, missing command, preference restoration'
