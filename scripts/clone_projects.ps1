param(
    [string]$Config = (Join-Path $PSScriptRoot '..\config\projects.yaml')
)

$ErrorActionPreference = 'Stop'
$git = (Get-Command git).Source
$entries = Select-String -LiteralPath $Config -Pattern "name: ([^,}]+), repo_url: '([^']+)', local_repo: '([^']+)'"
$failed = @()
foreach ($entry in $entries) {
    $name = $entry.Matches[0].Groups[1].Value.Trim()
    $url = $entry.Matches[0].Groups[2].Value
    $target = $entry.Matches[0].Groups[3].Value
    if (Test-Path -LiteralPath (Join-Path $target '.git')) {
        & $git -C $target rev-parse --verify HEAD 2>$null | Out-Null
        if ($LASTEXITCODE -eq 0) {
            Write-Host "[exists] $name"
            continue
        }
        Write-Host "[resume] $name"
        $symref = & $git -c http.version=HTTP/1.1 ls-remote --symref $url HEAD | Select-Object -First 1
        $head = (($symref -split "`t")[0] -replace '^ref: refs/heads/', '').Trim()
        & $git -c http.version=HTTP/1.1 -C $target fetch --filter=tree:0 --no-tags origin "refs/heads/${head}:refs/remotes/origin/${head}"
        if ($LASTEXITCODE -ne 0) { $failed += $name; continue }
        & $git -C $target symbolic-ref refs/remotes/origin/HEAD "refs/remotes/origin/$head"
        & $git -C $target symbolic-ref HEAD "refs/heads/$head"
        & $git -C $target branch --track $head "origin/$head"
        continue
    }
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $target) | Out-Null
    Write-Host "[clone] $name"
    & $git -c http.version=HTTP/1.1 clone --filter=tree:0 --no-tags --no-checkout --single-branch $url $target
    if ($LASTEXITCODE -ne 0) {
        $failed += $name
    }
}
if ($failed.Count -gt 0) {
    throw "Clone failed: $($failed -join ', ')"
}
