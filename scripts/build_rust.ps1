param(
    [switch]$Offline,
    [ValidateRange(1, 256)][int]$Jobs = 4
)

$ErrorActionPreference = "Stop"
$taskProjectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$taskPreviousLocation = Get-Location
try {
    Set-Location -LiteralPath $taskProjectRoot
    Write-Host "Building maximum-performance release with the default portable CPU target."
    $taskCargoArgs = @("build", "--release", "--locked", "--jobs", "$Jobs", "--target-dir", (Join-Path $taskProjectRoot "target"))
    if ($Offline) { $taskCargoArgs += "--offline" }
    & cargo @taskCargoArgs
    if ($LASTEXITCODE -ne 0) { throw "Rust release build failed (exit $LASTEXITCODE)" }
    Write-Host "Built target/release/prediction-market.exe; no trading process was started."
} finally {
    Set-Location -LiteralPath $taskPreviousLocation
}
