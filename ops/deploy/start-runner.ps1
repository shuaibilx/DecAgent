[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$RunnerDirectory,
    [Parameter(Mandatory = $true)][string]$ProjectDirectory
)

$ErrorActionPreference = 'Stop'
$RunnerDirectory = (Resolve-Path -LiteralPath $RunnerDirectory).Path
$ProjectDirectory = (Resolve-Path -LiteralPath $ProjectDirectory).Path
$runnerCommand = Join-Path $RunnerDirectory 'run.cmd'
if (-not (Test-Path -LiteralPath $runnerCommand -PathType Leaf)) {
    exit 1
}

$env:DECAGENT_LOCAL_PROJECT_DIR = $ProjectDirectory
$docker = Get-Command docker.exe -ErrorAction SilentlyContinue
if ($null -eq $docker) {
    exit 1
}

while ($true) {
    & $docker.Source info --format '{{.OSType}}' *> $null
    if ($LASTEXITCODE -eq 0) {
        & $runnerCommand
    }
    Start-Sleep -Seconds 15
}
