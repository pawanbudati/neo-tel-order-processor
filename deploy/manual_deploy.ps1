<#
.SYNOPSIS
    Manual Deployment Script for Consumer (Neo Kotak Order Processor) on Windows PowerShell.
    Matches GitHub Actions (.github/workflows/deploy.yml).

.DESCRIPTION
    Syncs repository files to GCP VMs (excluding .venv, .env, git, logs) and executes remote deploy.sh.
    Uses built-in Windows OpenSSH and tar.exe (or rsync if available).

.PARAMETER Target
    Deployment target: 'primary', 'yuva', or 'all' (default).

.PARAMETER VmHost
    Override Host IP/DNS for Primary VM.

.PARAMETER VmUser
    Override SSH User.

.PARAMETER SshKey
    Path to private SSH key.

.EXAMPLE
    .\deploy\manual_deploy.ps1
    .\deploy\manual_deploy.ps1 -Target primary
    .\deploy\manual_deploy.ps1 -Target yuva
    .\deploy\manual_deploy.ps1 -Target primary -VmHost "34.93.x.x" -VmUser "pawanbudati"
#>

[CmdletBinding()]
param(
    [ValidateSet('primary', 'yuva', 'all')]
    [string]$Target = 'all',

    [string]$VmHost = $null,
    [string]$VmUser = $null,
    [string]$SshKey = $null,
    [int]$Port = 0,
    [string]$DeployPath = $null
)

$ErrorActionPreference = 'Stop'

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$rootDir = (Resolve-Path "$scriptDir\..").Path

# Helper to load simple KEY=VALUE file
function Load-EnvFile($path) {
    if (Test-Path $path) {
        Write-Host "Loading configuration from $path" -ForegroundColor Cyan
        Get-Content $path | ForEach-Object {
            $line = $_.Trim()
            if ($line -and -not $line.StartsWith('#') -and $line.Contains('=')) {
                $idx = $line.IndexOf('=')
                $key = $line.Substring(0, $idx).Trim()
                $val = $line.Substring($idx + 1).Trim().Trim('"').Trim("'")
                if (-not [string]::IsNullOrEmpty($key)) {
                    [Environment]::SetEnvironmentVariable($key, $val, 'Process')
                }
            }
        }
    }
}

# Check for config files
$envCandidates = @(
    "$rootDir\.env.deploy",
    "$scriptDir\.env.deploy",
    "$rootDir\.deploy.env",
    "$scriptDir\deploy.env"
)
foreach ($f in $envCandidates) {
    if (Test-Path $f) {
        Load-EnvFile $f
        break
    }
}

function Invoke-DeployTarget {
    param(
        [string]$TargetName,
        [string]$HostName,
        [string]$UserName,
        [string]$KeyPath,
        [int]$PortNum,
        [string]$RemoteDir
    )

    if (-not $HostName -or -not $UserName) {
        Write-Error "Host and User must be configured for target '$TargetName'."
        return
    }

    if (-not $PortNum -or $PortNum -le 0) { $PortNum = 22 }
    if (-not $RemoteDir -or $RemoteDir -eq '/home/') {
        $RemoteDir = "/home/$UserName/apps/neo-tel-order-processor"
    }

    Write-Host "`n=================================================================" -ForegroundColor Green
    Write-Host ">>> Deploying Consumer to [$TargetName] at ${UserName}@${HostName}:${PortNum}" -ForegroundColor Green
    Write-Host ">>> Remote Path: $RemoteDir" -ForegroundColor Green
    Write-Host "=================================================================" -ForegroundColor Green

    $sshArgs = @("-p", "$PortNum", "-o", "StrictHostKeyChecking=accept-new")
    if ($KeyPath -and (Test-Path $KeyPath)) {
        $sshArgs += @("-i", $KeyPath)
    }

    # 1. Ensure target directory exists on remote VM
    & ssh $sshArgs "${UserName}@${HostName}" "mkdir -p '$RemoteDir'"
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to create remote directory '$RemoteDir' on $HostName."
    }

    # 2. Sync files using tar stream or rsync
    $hasRsync = (Get-Command rsync -ErrorAction SilentlyContinue)
    if ($hasRsync) {
        Write-Host ">>> Syncing files via rsync..." -ForegroundColor Yellow
        $rsyncSsh = "ssh -p $PortNum -o StrictHostKeyChecking=accept-new"
        if ($KeyPath -and (Test-Path $KeyPath)) { $rsyncSsh += " -i '$KeyPath'" }

        & rsync -az --delete `
            --exclude='.git/' `
            --exclude='.env' --include='.env.example' --exclude='.env.*' `
            --exclude='.venv/' --exclude='venv/' `
            --exclude='logs/' --exclude='*.log' `
            --exclude='__pycache__/' `
            -e $rsyncSsh `
            "$rootDir/" "${UserName}@${HostName}:${RemoteDir}/"
    } else {
        Write-Host ">>> Syncing files via tar over SSH stream..." -ForegroundColor Yellow
        # Build tar archive in memory and stream to remote ssh
        $excludes = @(
            "--exclude=.git",
            "--exclude=.env",
            "--exclude=.env.*",
            "--exclude=.venv",
            "--exclude=venv",
            "--exclude=logs",
            "--exclude=*.log",
            "--exclude=__pycache__"
        )
        
        $tarCmd = "tar -C `"$rootDir`" $($excludes -join ' ') -czf - ."
        $sshCmd = "ssh $($sshArgs -join ' ') ${UserName}@${HostName} `"tar -xzf - -C '$RemoteDir'`""
        cmd.exe /c "$tarCmd | $sshCmd"
    }

    if ($LASTEXITCODE -ne 0) {
        throw "File sync to $HostName failed."
    }

    # 3. Execute remote deploy.sh
    Write-Host ">>> Executing remote deploy.sh on $HostName..." -ForegroundColor Yellow
    & ssh $sshArgs "${UserName}@${HostName}" "DEPLOY_PATH='$RemoteDir' bash '$RemoteDir/deploy/deploy.sh'"
    if ($LASTEXITCODE -ne 0) {
        throw "Remote deploy.sh on $HostName reported an error."
    }

    Write-Host ">>> Deployment to [$TargetName] succeeded!" -ForegroundColor Green
}

# Resolve settings
$primaryHost = if ($VmHost) { $VmHost } elseif ($env:GCP_VM_HOST) { $env:GCP_VM_HOST } else { $env:VM_HOST }
$primaryUser = if ($VmUser) { $VmUser } elseif ($env:GCP_VM_USER) { $env:GCP_VM_USER } else { $env:VM_USER }
$primaryKey  = if ($SshKey) { $SshKey } elseif ($env:GCP_VM_SSH_KEY) { $env:GCP_VM_SSH_KEY } else { $env:SSH_KEY_PATH }
$primaryPort = if ($Port -gt 0) { $Port } elseif ($env:GCP_VM_SSH_PORT) { [int]$env:GCP_VM_SSH_PORT } elseif ($env:VM_PORT) { [int]$env:VM_PORT } else { 22 }
$primaryPath = if ($DeployPath) { $DeployPath } elseif ($env:CONSUMER_DEPLOY_PATH) { $env:CONSUMER_DEPLOY_PATH } else { $env:DEPLOY_PATH }

$yuvaHost = if ($env:GCP_VM_HOST_YUVA) { $env:GCP_VM_HOST_YUVA } else { $env:VM_HOST_YUVA }
$yuvaUser = if ($env:GCP_VM_USER_YUVA) { $env:GCP_VM_USER_YUVA } elseif ($env:VM_USER_YUVA) { $env:VM_USER_YUVA } else { $primaryUser }
$yuvaKey  = if ($env:GCP_VM_SSH_KEY_YUVA) { $env:GCP_VM_SSH_KEY_YUVA } elseif ($env:SSH_KEY_PATH_YUVA) { $env:SSH_KEY_PATH_YUVA } else { $primaryKey }
$yuvaPort = if ($env:GCP_VM_SSH_PORT_YUVA) { [int]$env:GCP_VM_SSH_PORT_YUVA } elseif ($env:VM_PORT_YUVA) { [int]$env:VM_PORT_YUVA } else { $primaryPort }
$yuvaPath = if ($env:CONSUMER_DEPLOY_PATH_YUVA) { $env:CONSUMER_DEPLOY_PATH_YUVA } elseif ($env:DEPLOY_PATH_YUVA) { $env:DEPLOY_PATH_YUVA } else { $primaryPath }

switch ($Target) {
    'primary' {
        Invoke-DeployTarget "Primary VM" $primaryHost $primaryUser $primaryKey $primaryPort $primaryPath
    }
    'yuva' {
        Invoke-DeployTarget "Yuva VM" $yuvaHost $yuvaUser $yuvaKey $yuvaPort $yuvaPath
    }
    'all' {
        if ($primaryHost) {
            Invoke-DeployTarget "Primary VM" $primaryHost $primaryUser $primaryKey $primaryPort $primaryPath
        }
        if ($yuvaHost) {
            Invoke-DeployTarget "Yuva VM" $yuvaHost $yuvaUser $yuvaKey $yuvaPort $yuvaPath
        }
        if (-not $primaryHost -and -not $yuvaHost) {
            Write-Error "No host configuration found. Define GCP_VM_HOST in .env.deploy or pass -VmHost."
        }
    }
}
