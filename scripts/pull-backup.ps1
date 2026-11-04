# Pull the lab data store backups from the primary node to this machine and keep the newest 4 of each kind.
# Needs: LAB_DATA_STORE_SSH = user@host of the primary node (environment variable); key-based ssh/scp access.
# Optional: LAB_DATA_STORE_BACKUP_DIR overrides the destination (default F:\Backups\lab-data-store).
# PowerShell 5.1 compatible.
$ErrorActionPreference = 'Stop'

$ssh = $env:LAB_DATA_STORE_SSH
if ([string]::IsNullOrWhiteSpace($ssh)) {
    Write-Error 'LAB_DATA_STORE_SSH is not set. Set it to user@host of the primary node, e.g. $env:LAB_DATA_STORE_SSH = "user@host".'
    exit 1
}

$dest = 'F:\Backups\lab-data-store'
if (-not [string]::IsNullOrWhiteSpace($env:LAB_DATA_STORE_BACKUP_DIR)) {
    $dest = $env:LAB_DATA_STORE_BACKUP_DIR
}
New-Item -ItemType Directory -Force -Path $dest | Out-Null

& scp -o BatchMode=yes "${ssh}:/srv/lab-data-store/backups/*" $dest
if ($LASTEXITCODE -ne 0) {
    Write-Error "scp failed with exit code $LASTEXITCODE"
    exit 1
}

$keep = 4
$patterns = @('lab-*.dump', 'globals-*.sql', 'files-*.tgz')
foreach ($pattern in $patterns) {
    $files = @(Get-ChildItem -Path $dest -Filter $pattern -File | Sort-Object Name -Descending)
    if ($files.Count -gt $keep) {
        $files | Select-Object -Skip $keep | Remove-Item -Force
    }
    $kept = @($files | Select-Object -First $keep)
    Write-Host "$pattern : kept $($kept.Count)"
    foreach ($f in $kept) { Write-Host "  $($f.Name)  $($f.Length) bytes" }
}

$sumFile = Join-Path $dest 'SHA256SUMS.txt'
$lines = foreach ($pattern in $patterns) {
    Get-ChildItem -Path $dest -Filter $pattern -File | Sort-Object Name | ForEach-Object {
        $hash = (Get-FileHash -Algorithm SHA256 -Path $_.FullName).Hash.ToLower()
        "$hash  $($_.Name)"
    }
}
Set-Content -Path $sumFile -Value $lines -Encoding ASCII
Write-Host "wrote $sumFile"
