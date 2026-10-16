[CmdletBinding()]
param(
    [string]$Repository = 'shuaibilx/DecAgent',
    [string]$RunnerDirectory = (Join-Path $env:USERPROFILE 'DecAgentRunner\actions-runner')
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$projectDirectory = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..\..')).Path
$envFile = Join-Path $projectDirectory '.env'
$secretsDirectory = Join-Path $projectDirectory '.decagent\secrets'
if (-not (Test-Path -LiteralPath $envFile -PathType Leaf)) {
    throw 'The project .env file is missing; create and configure the local deployment first.'
}
foreach ($secretName in @('broker_token', 'postgres_password', 'github_token', 'main_api_key', 'jev_api_key')) {
    if (-not (Test-Path -LiteralPath (Join-Path $secretsDirectory $secretName) -PathType Leaf)) {
        throw 'A required local Compose secret file is missing; refusing to register the Runner.'
    }
}
if (-not (Get-Command gh -ErrorAction SilentlyContinue)) {
    throw 'GitHub CLI is required. Sign in with an account that administers this repository.'
}
if (-not (Get-Command docker.exe -ErrorAction SilentlyContinue)) {
    throw 'Docker Desktop CLI is required.'
}

$authOutput = & gh auth status 2>&1
if ($LASTEXITCODE -ne 0) {
    throw 'GitHub CLI is not authenticated; sign in before installing the Runner.'
}

$runnerRoot = [System.IO.Path]::GetFullPath($RunnerDirectory)
New-Item -ItemType Directory -Path $runnerRoot -Force | Out-Null
$runnerMarker = Join-Path $runnerRoot '.runner'
if (-not (Test-Path -LiteralPath $runnerMarker -PathType Leaf)) {
    $legacyRunnerRoot = Join-Path $env:LOCALAPPDATA 'DecAgent\actions-runner'
    if (Test-Path -LiteralPath (Join-Path $legacyRunnerRoot '.runner') -PathType Leaf) {
        Get-ChildItem -LiteralPath $legacyRunnerRoot -Force | Copy-Item -Destination $runnerRoot -Recurse -Force
    }
}
if (-not (Test-Path -LiteralPath $runnerMarker -PathType Leaf)) {
    $releaseJson = & gh api repos/actions/runner/releases/latest
    if ($LASTEXITCODE -ne 0) {
        throw 'Could not read the official GitHub Actions Runner release metadata.'
    }
    $release = $releaseJson | ConvertFrom-Json
    $asset = $release.assets | Where-Object { $_.name -match '^actions-runner-win-x64-[0-9.]+\.zip$' } | Select-Object -First 1
    if ($null -eq $asset) {
        throw 'The latest official Runner release has no Windows x64 archive.'
    }

    $archivePath = Join-Path $env:TEMP $asset.name
    Invoke-WebRequest -Uri $asset.browser_download_url -OutFile $archivePath -UseBasicParsing
    if ($asset.digest -notmatch '^sha256:(?<hash>[0-9a-f]{64})$') {
        Remove-Item -LiteralPath $archivePath -Force
        throw 'The official Runner release did not provide a SHA-256 digest.'
    }
    $expectedHash = $Matches.hash
    $actualHash = (Get-FileHash -LiteralPath $archivePath -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actualHash -ne $expectedHash) {
        Remove-Item -LiteralPath $archivePath -Force
        throw 'The downloaded Runner archive does not match the official release digest.'
    }
    Expand-Archive -LiteralPath $archivePath -DestinationPath $runnerRoot -Force
    Remove-Item -LiteralPath $archivePath -Force

    $configScript = Join-Path $runnerRoot 'config.cmd'
    $registrationToken = (& gh api --method POST "repos/$Repository/actions/runners/registration-token" --jq .token).Trim()
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($registrationToken)) {
        throw 'Could not obtain a short-lived repository Runner registration token.'
    }

    $stdoutLog = Join-Path $runnerRoot 'configure.stdout.log'
    $stderrLog = Join-Path $runnerRoot 'configure.stderr.log'
    try {
        $configArguments = @(
            '--unattended',
            '--url', "https://github.com/$Repository",
            '--token', $registrationToken,
            '--name', 'decagent-local-deploy',
            '--labels', 'decagent-deploy',
            '--work', '_work',
            '--replace'
        )
        $process = Start-Process -FilePath $configScript -ArgumentList $configArguments -WorkingDirectory $runnerRoot -NoNewWindow -Wait -PassThru -RedirectStandardOutput $stdoutLog -RedirectStandardError $stderrLog
        if ($process.ExitCode -ne 0) {
            foreach ($logPath in @($stdoutLog, $stderrLog)) {
                if (Test-Path -LiteralPath $logPath) {
                    Get-Content -LiteralPath $logPath | ForEach-Object {
                        $_ -replace [regex]::Escape($registrationToken), '[REDACTED]'
                    }
                }
            }
            throw 'The repository Runner configuration failed; inspect the redacted output above.'
        }
    } finally {
        $registrationToken = $null
        Remove-Item -LiteralPath $stdoutLog, $stderrLog -Force -ErrorAction SilentlyContinue
    }
}

$runnerStartScript = Join-Path $runnerRoot 'start-runner.ps1'
$projectDirectoryFile = Join-Path $runnerRoot 'project-directory.txt'
$existingTask = Get-ScheduledTask -TaskName 'DecAgentLocalDeployRunner' -ErrorAction SilentlyContinue
if ($null -ne $existingTask) {
    Stop-ScheduledTask -TaskName 'DecAgentLocalDeployRunner' -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 2
}
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'start-runner.ps1') -Destination $runnerStartScript -Force
Set-Content -LiteralPath $projectDirectoryFile -Value $projectDirectory -Encoding UTF8
$windowsPowerShell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$identity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$scriptLiteral = "'" + $runnerStartScript.Replace("'", "''") + "'"
$runnerRootLiteral = "'" + $runnerRoot.Replace("'", "''") + "'"
$taskLogLiteral = "'" + (Join-Path $runnerRoot '_diag\local-task-launch.log').Replace("'", "''") + "'"
$taskStartupCommand = "Add-Content -LiteralPath $taskLogLiteral -Value 'Task PowerShell bootstrap started.' -Encoding UTF8; try { & $scriptLiteral -RunnerDirectory $runnerRootLiteral } catch { Add-Content -LiteralPath $taskLogLiteral -Value ('Task bootstrap failed with ' + `$_.Exception.GetType().Name) -Encoding UTF8; exit 1 }"
$encodedRunnerCommand = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($taskStartupCommand))
$actionArguments = '-NoLogo -NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -EncodedCommand {0}' -f $encodedRunnerCommand
$taskWorkingDirectory = Join-Path $env:SystemRoot 'System32'
$taskAction = New-ScheduledTaskAction -Execute $windowsPowerShell -Argument $actionArguments -WorkingDirectory $taskWorkingDirectory
$taskTrigger = New-ScheduledTaskTrigger -AtLogOn -User $identity
$taskSettings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName 'DecAgentLocalDeployRunner' -Action $taskAction -Trigger $taskTrigger -Settings $taskSettings -User $identity -RunLevel Limited -Description 'Runs the repository-scoped DecAgent release deployment Runner after this user signs in.' -Force | Out-Null
Start-ScheduledTask -TaskName 'DecAgentLocalDeployRunner'

$isOnline = $false
for ($attempt = 0; $attempt -lt 24; $attempt++) {
    Start-Sleep -Seconds 5
    $runnersJson = & gh api "repos/$Repository/actions/runners"
    if ($LASTEXITCODE -eq 0) {
        $runners = ($runnersJson | ConvertFrom-Json).runners
        $runner = $runners | Where-Object { $_.name -eq 'decagent-local-deploy' } | Select-Object -First 1
        if ($null -ne $runner -and $runner.status -eq 'online') {
            $isOnline = $true
            break
        }
    }
}
if (-not $isOnline) {
    throw 'Runner registration and logon task were created, but the Runner is not online yet. Confirm Docker Desktop is running and inspect the local Runner _diag logs.'
}

[pscustomobject]@{
    repository = $Repository
    runner = 'decagent-local-deploy'
    label = 'decagent-deploy'
    startup = 'current-user logon task; waits for Docker Desktop Linux engine'
    status = 'online'
} | ConvertTo-Json -Compress
