param(
    [string]$Server,
    [string]$RemoteDirectory,
    [ValidateRange(1, 65535)][int]$Port = 22,
    [string]$ConfigFile = (Join-Path (Split-Path -Parent $PSScriptRoot) 'deploy.local.json'),
    [string]$WslDistribution = 'Ubuntu-22.04',
    [ValidateRange(1, 256)][int]$Jobs = 4,
    [switch]$Offline,
    [switch]$BuildOnly
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$projectRoot = [IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
if (-not $BuildOnly) {
    if (Test-Path -LiteralPath $ConfigFile) {
        $config = Get-Content -LiteralPath $ConfigFile -Raw | ConvertFrom-Json
        foreach ($field in @('user', 'host', 'port', 'remote_directory')) {
            if (-not $config.PSObject.Properties[$field]) { throw "Missing '$field' in deployment config." }
        }
        if (-not $PSBoundParameters.ContainsKey('Server')) { $Server = "$($config.user)@$($config.host)" }
        if (-not $PSBoundParameters.ContainsKey('RemoteDirectory')) { $RemoteDirectory = $config.remote_directory }
        if (-not $PSBoundParameters.ContainsKey('Port')) { $Port = [int]$config.port }
        if (-not $PSBoundParameters.ContainsKey('WslDistribution') -and $config.PSObject.Properties['wsl_distribution']) {
            $WslDistribution = $config.wsl_distribution
        }
    } elseif ($PSBoundParameters.ContainsKey('ConfigFile')) { throw 'Deployment config was not found.' }
    if ($Port -lt 1 -or $Port -gt 65535) { throw 'Invalid SSH port.' }
    if ($Server -notmatch '^[A-Za-z0-9_][A-Za-z0-9_.-]*@[A-Za-z0-9][A-Za-z0-9.-]*$') {
        throw 'Set user/host in deploy.local.json, or pass -Server user@hostname.'
    }
    if ($RemoteDirectory -notmatch '^/[A-Za-z0-9_./-]+$' -or $RemoteDirectory -eq '/' -or ($RemoteDirectory -split '/') -contains '..') {
        throw 'RemoteDirectory must be an absolute Linux path without spaces or .. segments.'
    }
}
if ([string]::IsNullOrWhiteSpace($WslDistribution) -or $WslDistribution.Contains("`n") -or $WslDistribution.Contains("`r")) {
    throw 'Invalid WSL distribution name.'
}
$tools = @('wsl.exe')
if (-not $BuildOnly) { $tools += @('ssh.exe', 'scp.exe') }
foreach ($tool in $tools) {
    if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) { throw "Missing $tool." }
}
function Invoke-Checked {
    param([string]$Tool, [string[]]$Arguments)
    & $Tool @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$Tool failed with exit code $LASTEXITCODE." }
}
function Get-WslPath {
    param([string]$Path)
    $result = (& wsl.exe --distribution $WslDistribution --exec wslpath -a -u $Path | Out-String).Trim()
    if ($LASTEXITCODE -ne 0 -or -not $result.StartsWith('/')) { throw 'WSL path conversion failed.' }
    return $result
}

$distRoot = Join-Path $projectRoot 'dist'
$stage = Join-Path $distRoot ('rust-upload-staging-' + [guid]::NewGuid().ToString('N'))
$archive = Join-Path $distRoot 'prediction-market-rust-ubuntu22-to24-x86_64.tar.gz'
New-Item -ItemType Directory -Path $stage -Force | Out-Null
try {
    $rootWsl = Get-WslPath $projectRoot
    $stageWsl = Get-WslPath $stage
    $archiveWsl = Get-WslPath $archive
    $offlineArgument = if ($Offline) { '1' } else { '0' }
    Invoke-Checked 'wsl.exe' @('--distribution', $WslDistribution, '--exec', 'bash',
        "$rootWsl/scripts/build_rust_wsl.sh", $rootWsl, $stageWsl, $archiveWsl, "$Jobs", $offlineArgument)
    $hashAlgorithm = [Security.Cryptography.SHA256]::Create()
    $archiveStream = [IO.File]::OpenRead($archive)
    try { $archiveHash = [BitConverter]::ToString($hashAlgorithm.ComputeHash($archiveStream)).Replace('-', '').ToLowerInvariant() }
    finally { $archiveStream.Dispose(); $hashAlgorithm.Dispose() }
    [IO.File]::WriteAllText("$archive.sha256", "$archiveHash  $([IO.Path]::GetFileName($archive))`n", [Text.UTF8Encoding]::new($false))
    Write-Host "Linux archive ready: $archive"
    if ($BuildOnly) { Write-Host 'BuildOnly completed; no SSH, upload or installation was performed.'; return }

    # Build finishes before contacting the server. Strict path checks keep the
    # remote command arguments literal and prohibit shell metacharacters.
    $remoteStage = (& ssh.exe -p $Port -- $Server 'mktemp -d /tmp/prediction-rust-upload.XXXXXXXX' | Out-String).Trim()
    if ($LASTEXITCODE -ne 0 -or $remoteStage -notmatch '^/tmp/prediction-rust-upload\.[A-Za-z0-9]+$') {
        throw 'Could not create a remote upload staging directory.'
    }
    Invoke-Checked 'scp.exe' @('-P', "$Port", '--', $archive, "$archive.sha256",
        (Join-Path $stage 'scripts/install_rust_remote.sh'), (Join-Path $stage 'scripts/rust_release.py'), "${Server}:$remoteStage/")
    $archiveName = [IO.Path]::GetFileName($archive)
    $remoteCommand = "cd '$remoteStage' && sha256sum -c '$archiveName.sha256' && bash '$remoteStage/install_rust_remote.sh' '$RemoteDirectory' '$remoteStage/$archiveName' '$remoteStage/rust_release.py'"
    Invoke-Checked 'ssh.exe' @('-t', '-p', "$Port", '--', $Server, $remoteCommand)
    Write-Host "Uploaded Linux Rust release to ${Server}:$RemoteDirectory. No live trading was started."
} finally {
    $resolvedStage = [IO.Path]::GetFullPath($stage)
    $allowedPrefix = [IO.Path]::GetFullPath($distRoot) + [IO.Path]::DirectorySeparatorChar
    if ($resolvedStage.StartsWith($allowedPrefix, [StringComparison]::OrdinalIgnoreCase) -and (Test-Path -LiteralPath $resolvedStage)) {
        Remove-Item -LiteralPath $resolvedStage -Recurse -Force
    }
}
