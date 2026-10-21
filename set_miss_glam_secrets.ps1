# Copies the Miss Glam portal login into this repo's GitHub Actions secrets. YOU run this - it sends your
# credentials to GitHub.
#
#   .\set_miss_glam_secrets.ps1
#
# Reads Automation\MissGlam-Sales\.env (GLAMINDO_USERNAME / GLAMINDO_PASSWORD). No value is printed or put on a
# command line: the logins go through a temporary dotenv file (gh secret set -f) that is deleted afterwards.
# GCP_SA_KEY is already set by set_secrets.ps1.
[CmdletBinding()]
param([string] $Repo = "brianirsyad-scarlett/som-minimarket-automation")

$ErrorActionPreference = "Stop"
$envFile = "D:\SCARLETT_512\SCARLETT-329\SOM\Automation\MissGlam-Sales\.env"
if (-not (Test-Path $envFile)) { throw "Missing $envFile" }
$h = @{}
foreach ($line in Get-Content $envFile) {
    if ($line -match '^\s*#' -or $line -notmatch '=') { continue }
    $k, $v = $line -split '=', 2
    $h[$k.Trim()] = $v.Trim()
}
$secrets = [ordered]@{
    GLAMINDO_USERNAME = $h["GLAMINDO_USERNAME"]
    GLAMINDO_PASSWORD = $h["GLAMINDO_PASSWORD"]
}
$missing = @($secrets.Keys | Where-Object { [string]::IsNullOrWhiteSpace($secrets[$_]) })
if ($missing) { throw "Empty in the .env: $($missing -join ', ')" }

$tmp = Join-Path $env:TEMP ("som_secrets_{0}.env" -f [guid]::NewGuid().ToString("N"))
try {
    $lines = foreach ($k in $secrets.Keys) {
        $v = $secrets[$k].Replace('\', '\\').Replace('"', '\"')
        "$k=`"$v`""
    }
    [IO.File]::WriteAllLines($tmp, $lines)     # UTF-8, no BOM
    gh secret set -f $tmp --repo $Repo
    if ($LASTEXITCODE -ne 0) { throw "gh secret set failed" }
} finally {
    Remove-Item $tmp -Force -ErrorAction SilentlyContinue
}
Write-Host "Secrets now on $Repo (names only):" -ForegroundColor Green
gh secret list --repo $Repo
