<#
.SYNOPSIS
    Manual Deployment Script for Consumer (Neo Kotak Order Processor) on Windows PowerShell.
    Matches GitHub Actions (.github/workflows/deploy.yml).

.DESCRIPTION
    Syncs repository files to GCP VMs (excluding .venv, .env, git, logs) and executes remote deploy.sh.
    Uses built-in Windows OpenSSH and tar.exe (or rsync if available).

.PARAMETER Target
    Deployment target: 'primary', 'yuva', 'pavan', 'sahi', or 'all' (default).

.EXAMPLE
    .\deploy\manual_deploy.ps1
    .\deploy\manual_deploy.ps1 -Target pavan
    .\deploy\manual_deploy.ps1 -Target sahi
#>

[CmdletBinding()]
param(
    [ValidateSet('primary', 'yuva', 'pavan', 'sahi', 'all')]
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
        Write-Host "Skipping [$TargetName]: Host or User is not configured." -ForegroundColor Yellow
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

# Resolve defaults
$defaultUser = if ($VmUser) { $VmUser } elseif ($env:GCP_VM_USER) { $env:GCP_VM_USER } else { $env:VM_USER }
$defaultKey  = if ($SshKey) { $SshKey } elseif ($env:GCP_VM_SSH_KEY) { $env:GCP_VM_SSH_KEY } else { $env:SSH_KEY_PATH }
$defaultPort = if ($Port -gt 0) { $Port } elseif ($env:GCP_VM_SSH_PORT) { [int]$env:GCP_VM_SSH_PORT } elseif ($env:VM_PORT) { [int]$env:VM_PORT } else { 22 }
$defaultPath = if ($DeployPath) { $DeployPath } elseif ($env:CONSUMER_DEPLOY_PATH) { $env:CONSUMER_DEPLOY_PATH } else { $env:DEPLOY_PATH }

# Primary VM
$primaryHost = if ($VmHost) { $VmHost } elseif ($env:GCP_VM_HOST) { $env:GCP_VM_HOST } else { $env:VM_HOST }
$primaryUser = $defaultUser
$primaryKey  = $defaultKey
$primaryPort = $defaultPort
$primaryPath = $defaultPath

# Yuva VM
$yuvaHost = if ($env:GCP_VM_HOST_YUVA) { $env:GCP_VM_HOST_YUVA } else { $env:VM_HOST_YUVA }
$yuvaUser = if ($env:GCP_VM_USER_YUVA) { $env:GCP_VM_USER_YUVA } else { $defaultUser }
$yuvaKey  = if ($env:GCP_VM_SSH_KEY_YUVA) { $env:GCP_VM_SSH_KEY_YUVA } else { $defaultKey }
$yuvaPort = if ($env:GCP_VM_SSH_PORT_YUVA) { [int]$env:GCP_VM_SSH_PORT_YUVA } else { $defaultPort }
$yuvaPath = if ($env:CONSUMER_DEPLOY_PATH_YUVA) { $env:CONSUMER_DEPLOY_PATH_YUVA } else { $defaultPath }

# Pavan VM
$pavanHost = if ($env:GCP_VM_HOST_PAVAN) { $env:GCP_VM_HOST_PAVAN } else { $env:VM_HOST_PAVAN }
$pavanUser = if ($env:GCP_VM_USER_PAVAN) { $env:GCP_VM_USER_PAVAN } else { $defaultUser }
$pavanKey  = if ($env:GCP_VM_SSH_KEY_PAVAN) { $env:GCP_VM_SSH_KEY_PAVAN } else { $defaultKey }
$pavanPort = if ($env:GCP_VM_SSH_PORT_PAVAN) { [int]$env:GCP_VM_SSH_PORT_PAVAN } else { $defaultPort }
$pavanPath = if ($env:CONSUMER_DEPLOY_PATH_PAVAN) { $env:CONSUMER_DEPLOY_PATH_PAVAN } else { $defaultPath }

# Sahi VM
$sahiHost = if ($env:GCP_VM_HOST_SAHI) { $env:GCP_VM_HOST_SAHI } else { $env:VM_HOST_SAHI }
$sahiUser = if ($env:GCP_VM_USER_SAHI) { $env:GCP_VM_USER_SAHI } else { $defaultUser }
$sahiKey  = if ($env:GCP_VM_SSH_KEY_SAHI) { $env:GCP_VM_SSH_KEY_SAHI } else { $defaultKey }
$sahiPort = if ($env:GCP_VM_SSH_PORT_SAHI) { [int]$env:GCP_VM_SSH_PORT_SAHI } else { $defaultPort }
$sahiPath = if ($env:CONSUMER_DEPLOY_PATH_SAHI) { $env:CONSUMER_DEPLOY_PATH_SAHI } else { $defaultPath }

switch ($Target) {
    'primary' {
        Invoke-DeployTarget "Primary VM" $primaryHost $primaryUser $primaryKey $primaryPort $primaryPath
    }
    'yuva' {
        Invoke-DeployTarget "Yuva VM" $yuvaHost $yuvaUser $yuvaKey $yuvaPort $yuvaPath
    }
    'pavan' {
        Invoke-DeployTarget "Pavan VM" $pavanHost $pavanUser $pavanKey $pavanPort $pavanPath
    }
    'sahi' {
        Invoke-DeployTarget "Sahi VM" $sahiHost $sahiUser $sahiKey $sahiPort $sahiPath
    }
    'all' {
        if ($primaryHost) { Invoke-DeployTarget "Primary VM" $primaryHost $primaryUser $primaryKey $primaryPort $primaryPath }
        if ($yuvaHost)    { Invoke-DeployTarget "Yuva VM" $yuvaHost $yuvaUser $yuvaKey $yuvaPort $yuvaPath }
        if ($pavanHost)   { Invoke-DeployTarget "Pavan VM" $pavanHost $pavanUser $pavanKey $pavanPort $pavanPath }
        if ($sahiHost)    { Invoke-DeployTarget "Sahi VM" $sahiHost $sahiUser $sahiKey $sahiPort $sahiPath }
    }
}
