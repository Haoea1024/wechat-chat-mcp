# Windows PowerShell can turn redirected native stderr into terminating errors.
# Keep diagnostics, but decide native success solely from the process exit code.
function Invoke-WeChatNative {
    param([string]$Executable, [string[]]$Arguments, [string]$FailureMessage)
    $null = Get-Command $Executable -ErrorAction Stop
    $previousPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        $global:LASTEXITCODE = 0
        & $Executable @Arguments 2>&1 | ForEach-Object { Write-Host $_.ToString() }
        $nativeExit = $LASTEXITCODE
        if ($nativeExit -ne 0) { throw "$FailureMessage (exit $nativeExit)" }
    } finally {
        $ErrorActionPreference = $previousPreference
    }
}
