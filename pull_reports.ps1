# Copy the reports made on the Ubuntu machine back to this folder.
# Windows runs this at logon and every hour (scheduled task "X Daily pull"); it is safe to run by hand.
$ErrorActionPreference = 'Stop'
$remote = 'len80t-via-relay'
$root = $PSScriptRoot
$log = Join-Path $root 'pull.log'

function Log($text) { Add-Content -Path $log -Value "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $text" -Encoding utf8 }

try {
    foreach ($folder in 'reports', 'data') {
        $before = @(Get-ChildItem (Join-Path $root $folder) -File -ErrorAction SilentlyContinue | ForEach-Object Name)
        scp -q -r -o BatchMode=yes -o ConnectTimeout=20 "${remote}:projects/X_Daily/$folder" $root
        if ($LASTEXITCODE -ne 0) { throw "scp $folder exited with $LASTEXITCODE" }
        $new = @(Get-ChildItem (Join-Path $root $folder) -File | ForEach-Object Name | Where-Object { $_ -notin $before })
        if ($folder -eq 'reports' -and $new) { Log "new: $($new -join ', ')" }
    }
} catch {
    Log "failed: $_"  # Usually the Ubuntu machine or the relay is offline; the next run catches up.
    exit 1
}
