[CmdletBinding()]
param(
    [string]$Repository = 'shuaibilx/DecAgent-deploy',
    [string]$RunnerDirectory = (Join-Path $env:USERPROFILE 'DecAgentPrivateDeployRunner\actions-runner')
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ApprovedRepository = 'shuaibilx/DecAgent-deploy'
if ($Repository -cne $ApprovedRepository) {
    throw 'The local Runner may only target the private DecAgent deployment repository.'
}

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

$repositoryPrivateOutput = & gh api "repos/$Repository" --jq .private 2>$null
$repositoryPrivateExitCode = $LASTEXITCODE
$repositoryPrivate = ([string]$repositoryPrivateOutput).Trim()
if ($repositoryPrivateExitCode -ne 0 -or $repositoryPrivate -cne 'true') {
    throw 'The approved deployment repository must exist and be private; refusing to register a Runner.'
}
$registeredRunnersJson = & gh api "repos/$Repository/actions/runners"
if ($LASTEXITCODE -ne 0) {
    throw 'Could not verify Runner registrations in the private deployment repository.'
}
$registeredRunners = @((($registeredRunnersJson | ConvertFrom-Json).runners))
$runnerName = 'decagent-local-deploy'
$registeredRunnerByName = @($registeredRunners | Where-Object { $_.name -eq $runnerName })

$runnerRoot = [System.IO.Path]::GetFullPath($RunnerDirectory)
$runnerMarker = Join-Path $runnerRoot '.runner'
if (Test-Path -LiteralPath $runnerMarker -PathType Leaf) {
    try {
        $runnerConfig = Get-Content -LiteralPath $runnerMarker -Raw | ConvertFrom-Json
    } catch {
        throw 'Existing Runner configuration is unreadable; refusing to start it.'
    }
    if ($runnerConfig.gitHubUrl -cne "https://github.com/$Repository" -or $runnerConfig.agentName -cne $runnerName) {
        throw 'Existing Runner configuration does not target the approved private repository.'
    }
    $registeredRunnerById = @(
        $registeredRunners | Where-Object { [string]$_.id -eq [string]$runnerConfig.agentId }
    )
    if ($registeredRunnerById.Count -ne 1 -or $registeredRunnerById[0].name -ne $runnerName) {
        throw 'Existing Runner ID is not registered in the approved private repository.'
    }
} else {
    if ($registeredRunnerByName.Count -gt 0) {
        throw 'A private deployment Runner with this name already exists; refusing to replace it.'
    }
    if (Test-Path -LiteralPath $runnerRoot -PathType Container) {
        $unexpectedEntry = Get-ChildItem -LiteralPath $runnerRoot -Force | Select-Object -First 1
        if ($null -ne $unexpectedEntry) {
            throw 'Runner directory must be empty; refusing to reuse unverified local files.'
        }
    }
}

if (-not (Test-Path -LiteralPath $runnerMarker -PathType Leaf)) {
    New-Item -ItemType Directory -Path $runnerRoot -Force | Out-Null
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
            '--work', '_work'
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
$runnerTaskName = 'DecAgentPrivateDeployRunner'
$existingTask = Get-ScheduledTask -TaskName $runnerTaskName -ErrorAction SilentlyContinue
if ($null -ne $existingTask) {
    Stop-ScheduledTask -TaskName $runnerTaskName -ErrorAction SilentlyContinue
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
Register-ScheduledTask -TaskName $runnerTaskName -Action $taskAction -Trigger $taskTrigger -Settings $taskSettings -User $identity -RunLevel Limited -Description 'Runs the private-repository DecAgent deployment Runner after this user signs in.' -Force | Out-Null
Start-ScheduledTask -TaskName $runnerTaskName

$isOnline = $false
for ($attempt = 0; $attempt -lt 24; $attempt++) {
    Start-Sleep -Seconds 5
    $runnersJson = & gh api "repos/$Repository/actions/runners"
    if ($LASTEXITCODE -eq 0) {
        $runners = ($runnersJson | ConvertFrom-Json).runners
        $runner = $runners | Where-Object { $_.name -eq $runnerName } | Select-Object -First 1
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
    runner = $runnerName
    label = 'decagent-deploy'
    startup = 'current-user logon task; waits for Docker Desktop Linux engine'
    status = 'online'
} | ConvertTo-Json -Compress
