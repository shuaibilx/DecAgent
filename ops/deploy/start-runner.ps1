[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$RunnerDirectory
)

$ErrorActionPreference = 'Stop'
$bootstrapLog = Join-Path $RunnerDirectory '_diag\local-bootstrap.log'
New-Item -ItemType Directory -Path (Split-Path -Parent $bootstrapLog) -Force | Out-Null

function Write-BootstrapLog {
    param([Parameter(Mandatory = $true)][string]$Message)
    $timestamp = [DateTime]::UtcNow.ToString('o')
    Add-Content -LiteralPath $bootstrapLog -Value "$timestamp $Message" -Encoding UTF8
}

try {
    $RunnerDirectory = (Resolve-Path -LiteralPath $RunnerDirectory).Path
    $projectDirectoryFile = Join-Path $RunnerDirectory 'project-directory.txt'
    if (-not (Test-Path -LiteralPath $projectDirectoryFile -PathType Leaf)) {
        throw 'Project directory configuration is missing.'
    }
    $ProjectDirectory = (Get-Content -LiteralPath $projectDirectoryFile -Raw -Encoding UTF8).Trim()
    $ProjectDirectory = (Resolve-Path -LiteralPath $ProjectDirectory).Path
    $runnerCommand = Join-Path $RunnerDirectory 'run.cmd'
    if (-not (Test-Path -LiteralPath $runnerCommand -PathType Leaf)) {
        throw 'Runner command file is missing.'
    }
} catch {
    Write-BootstrapLog "Runner bootstrap configuration failed with $($_.Exception.GetType().Name)."
    exit 1
}

$env:DECAGENT_LOCAL_PROJECT_DIR = $ProjectDirectory
$dockerCommand = Get-Command docker.exe -ErrorAction SilentlyContinue
if ($null -ne $dockerCommand) {
    $dockerPath = $dockerCommand.Source
} else {
    $dockerPath = Join-Path $env:ProgramFiles 'Docker\Docker\resources\bin\docker.exe'
    if (-not (Test-Path -LiteralPath $dockerPath -PathType Leaf)) {
        Write-BootstrapLog 'Docker CLI was not found in PATH or its standard installation directory.'
        exit 1
    }
}

$engineReadyLogged = $false
Set-Location -LiteralPath $RunnerDirectory
Write-BootstrapLog 'Runner bootstrap loop started.'
while ($true) {
    $dockerOs = & $dockerPath info --format '{{.OSType}}' 2>$null
    if ($LASTEXITCODE -eq 0 -and $dockerOs.Trim() -eq 'linux') {
        if (-not $engineReadyLogged) {
            Write-BootstrapLog 'Docker Desktop Linux engine is ready.'
            $engineReadyLogged = $true
        }
        Push-Location -LiteralPath $RunnerDirectory
        try {
            & $runnerCommand
            Write-BootstrapLog "Runner listener exited with code $LASTEXITCODE; retrying after delay."
        } catch {
            Write-BootstrapLog "Runner command failed with $($_.Exception.GetType().Name); retrying after delay."
        } finally {
            Pop-Location
        }
    } elseif ($engineReadyLogged) {
        Write-BootstrapLog 'Docker Desktop Linux engine is unavailable; waiting.'
        $engineReadyLogged = $false
    }
    Start-Sleep -Seconds 15
}
