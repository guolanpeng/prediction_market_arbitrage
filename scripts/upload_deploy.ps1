param(
    [string]$Server,
    [string]$RemoteDirectory,
    [ValidateRange(1, 65535)][int]$Port = 22,
    [string]$ConfigFile = (Join-Path (Split-Path -Parent $PSScriptRoot) 'deploy.local.json')
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
if (Test-Path -LiteralPath $ConfigFile) {
    $config = Get-Content -LiteralPath $ConfigFile -Raw | ConvertFrom-Json
    foreach ($field in @('user', 'host', 'port', 'remote_directory')) {
        if (-not $config.PSObject.Properties[$field]) {
            throw "Missing '$field' in deployment config: $ConfigFile"
        }
    }
    if (-not $PSBoundParameters.ContainsKey('Server')) { $Server = "$($config.user)@$($config.host)" }
    if (-not $PSBoundParameters.ContainsKey('RemoteDirectory')) { $RemoteDirectory = $config.remote_directory }
    if (-not $PSBoundParameters.ContainsKey('Port')) { $Port = [int]$config.port }
} elseif ($PSBoundParameters.ContainsKey('ConfigFile')) {
    throw "Deployment config not found: $ConfigFile"
}
if ($Port -lt 1 -or $Port -gt 65535) { throw 'SSH port must be between 1 and 65535.' }
if ($Server -notmatch '^[A-Za-z0-9_][A-Za-z0-9_.-]*@[A-Za-z0-9][A-Za-z0-9.-]*$') {
    throw 'Set user/host in deploy.local.json, or pass -Server user@hostname.'
}
if ($RemoteDirectory -notmatch '^/[A-Za-z0-9_./-]+$' -or
    $RemoteDirectory -eq '/' -or ($RemoteDirectory -split '/') -contains '..') {
    throw 'RemoteDirectory must be an absolute Linux path without spaces or .. segments.'
}
foreach ($tool in @('ssh.exe', 'scp.exe', 'tar.exe')) {
    if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) {
        throw "Missing $tool. Install Windows OpenSSH Client and tar before continuing."
    }
}

function Invoke-Checked {
    param([string]$Tool, [string[]]$Arguments)
    & $Tool @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$Tool failed with exit code $LASTEXITCODE." }
}

$projectRoot = Split-Path -Parent $PSScriptRoot
$distRoot = Join-Path $projectRoot 'dist'
$stage = Join-Path $distRoot ('upload-staging-' + [guid]::NewGuid().ToString('N'))
$archive = Join-Path $distRoot 'prediction-market-arbitrage-upload.tar.gz'
New-Item -ItemType Directory -Path $stage -Force | Out-Null
try {
    $files = @('README.md', 'UBUNTU.md', 'AGENTS.md', '.env.example', '.python-version',
        '.gitignore', '.gitattributes', 'python/pyproject.toml', 'python/uv.lock',
        'python/requirements.txt', 'scripts/deploy_ubuntu.sh', 'scripts/install_remote.sh', 'scripts/upload_deploy.ps1', 'deploy.example.json')
    $files += Get-ChildItem -LiteralPath (Join-Path $projectRoot 'user_strategies') -Recurse -File |
        Where-Object { $_.Extension -in @('.py', '.md') -and $_.FullName -notmatch '[\\/]__pycache__[\\/]' } |
        ForEach-Object { $_.FullName.Substring($projectRoot.Length + 1).Replace('\', '/') }
    $checksums = @()
    foreach ($relative in $files) {
        $destination = Join-Path $stage $relative
        New-Item -ItemType Directory -Path (Split-Path -Parent $destination) -Force | Out-Null
        Copy-Item -LiteralPath (Join-Path $projectRoot $relative) -Destination $destination
        $checksums += (Get-FileHash -LiteralPath $destination -Algorithm SHA256).Hash.ToLowerInvariant() + '  ' + $relative
    }
    [System.IO.File]::WriteAllText((Join-Path $stage 'SHA256SUMS'), ($checksums -join "`n") + "`n",
        [System.Text.UTF8Encoding]::new($false))
    Invoke-Checked -Tool 'tar.exe' -Arguments @('-czf', $archive, '-C', $stage, '.')
    Write-Host "Code archive ready: $archive (credentials, runtime data and virtual environments excluded)."

    $remoteStage = (& ssh.exe -p $Port -- $Server 'mktemp -d /tmp/prediction-upload.XXXXXXXX' | Out-String).Trim()
    if ($LASTEXITCODE -ne 0 -or $remoteStage -notmatch '^/tmp/prediction-upload\.[A-Za-z0-9]+$') {
        throw 'Could not create a deployment staging directory on the server.'
    }
    Invoke-Checked -Tool 'scp.exe' -Arguments @('-P', "$Port", '--', $archive,
        (Join-Path $PSScriptRoot 'install_remote.sh'), "${Server}:$remoteStage/")
    $remoteCommand = "bash '$remoteStage/install_remote.sh' '$RemoteDirectory' '$remoteStage/prediction-market-arbitrage-upload.tar.gz'"
    Invoke-Checked -Tool 'ssh.exe' -Arguments @('-t', '-p', "$Port", '--', $Server, $remoteCommand)
    Write-Host "Uploaded and installed at ${Server}:$RemoteDirectory. No live trading was started."
} finally {
    $resolvedStage = [System.IO.Path]::GetFullPath($stage)
    $allowedPrefix = [System.IO.Path]::GetFullPath($distRoot) + [System.IO.Path]::DirectorySeparatorChar
    if ($resolvedStage.StartsWith($allowedPrefix, [System.StringComparison]::OrdinalIgnoreCase) -and
        (Test-Path -LiteralPath $resolvedStage)) {
        Remove-Item -LiteralPath $resolvedStage -Recurse -Force
    }
}
