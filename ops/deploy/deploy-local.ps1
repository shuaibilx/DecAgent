[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$ProjectDirectory,

    [Parameter(Mandatory = $true)]
    [string]$SourceDirectory,

    [Parameter(Mandatory = $true)]
    [ValidatePattern('^v[0-9]+\.[0-9]+\.[0-9]+$')]
    [string]$ReleaseVersion,

    [Parameter(Mandatory = $true)]
    [ValidatePattern('^ghcr\.io/[a-z0-9._-]+/[a-z0-9._-]+@sha256:[0-9a-f]{64}$')]
    [string]$ControlImage,

    [Parameter(Mandatory = $true)]
    [ValidatePattern('^ghcr\.io/[a-z0-9._-]+/[a-z0-9._-]+@sha256:[0-9a-f]{64}$')]
    [string]$ExecutorImage,

    [Parameter(Mandatory = $true)]
    [ValidatePattern('^ghcr\.io/[a-z0-9._-]+/[a-z0-9._-]+@sha256:[0-9a-f]{64}$')]
    [string]$RunnerImage,

    [switch]$ValidateOnly
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Get-DotEnvValue {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Name,
        [string]$DefaultValue = ''
    )

    $pattern = '^\s*' + [regex]::Escape($Name) + '\s*='
    $line = Get-Content -LiteralPath $Path | Where-Object { $_ -match $pattern } | Select-Object -Last 1
    if ($null -eq $line) {
        return $DefaultValue
    }

    $value = $line.Substring($line.IndexOf('=') + 1).Trim()
    if ($value.Length -ge 2 -and (($value[0] -eq '"' -and $value[$value.Length - 1] -eq '"') -or ($value[0] -eq "'" -and $value[$value.Length - 1] -eq "'"))) {
        $value = $value.Substring(1, $value.Length - 2)
    }
    return $value
}

function Invoke-Compose {
    param([Parameter(Mandatory = $true)][string[]]$Arguments)

    $completeArguments = @($script:ComposeBaseArguments) + @($Arguments)
    & docker compose @completeArguments
    if ($LASTEXITCODE -ne 0) {
        throw "Docker Compose command failed: $($Arguments[0])."
    }
}

function Invoke-ComposeCapture {
    param([Parameter(Mandatory = $true)][string[]]$Arguments)

    $completeArguments = @($script:ComposeBaseArguments) + @($Arguments)
    $output = & docker compose @completeArguments
    if ($LASTEXITCODE -ne 0) {
        throw "Docker Compose command failed: $($Arguments[0])."
    }
    return ($output -join [Environment]::NewLine)
}

$databaseBackup = $null
$backupCreated = $false
$temporaryDatabaseBackup = $null

try {
    $ProjectDirectory = (Resolve-Path -LiteralPath $ProjectDirectory).Path
    $SourceDirectory = (Resolve-Path -LiteralPath $SourceDirectory).Path
    $envFile = Join-Path $ProjectDirectory '.env'
    $baseCompose = Join-Path $SourceDirectory 'compose.yaml'
    $windowsCompose = Join-Path $SourceDirectory 'compose.windows.yaml'

    if (-not (Test-Path -LiteralPath $envFile -PathType Leaf)) {
        throw 'The local .env file is missing; refusing to deploy.'
    }
    if (-not (Test-Path -LiteralPath $baseCompose -PathType Leaf) -or -not (Test-Path -LiteralPath $windowsCompose -PathType Leaf)) {
        throw 'The release checkout is missing the required Compose files.'
    }

    $secretsSetting = $env:DECAGENT_SECRETS_DIR
    if ([string]::IsNullOrWhiteSpace($secretsSetting)) {
        $secretsSetting = Get-DotEnvValue -Path $envFile -Name 'DECAGENT_SECRETS_DIR' -DefaultValue '.decagent/secrets'
    }
    if ([System.IO.Path]::IsPathRooted($secretsSetting)) {
        $secretsDirectory = $secretsSetting
    } else {
        $secretsDirectory = Join-Path $ProjectDirectory $secretsSetting
    }
    foreach ($secretName in @('broker_token', 'postgres_password', 'github_token', 'main_api_key', 'jev_api_key')) {
        if (-not (Test-Path -LiteralPath (Join-Path $secretsDirectory $secretName) -PathType Leaf)) {
            throw 'A required local Compose secret file is missing; refusing to deploy.'
        }
    }

    $apiPortValue = $env:DECAGENT_API_PORT
    if ([string]::IsNullOrWhiteSpace($apiPortValue)) {
        $apiPortValue = Get-DotEnvValue -Path $envFile -Name 'DECAGENT_API_PORT' -DefaultValue '8000'
    }
    $apiPort = 0
    if (-not [int]::TryParse($apiPortValue, [ref]$apiPort) -or $apiPort -lt 1 -or $apiPort -gt 65535) {
        throw 'DECAGENT_API_PORT must be a valid TCP port.'
    }

    $dockerOs = & docker info --format '{{.OSType}}'
    if ($LASTEXITCODE -ne 0 -or $dockerOs.Trim() -ne 'linux') {
        throw 'Docker Desktop must be running with its Linux container engine.'
    }

    $env:DECAGENT_CONTROL_IMAGE = $ControlImage
    $env:DECAGENT_EXECUTOR_IMAGE = $ExecutorImage
    $env:DECAGENT_RUNNER_IMAGE = $RunnerImage
    $script:ComposeBaseArguments = @(
        '--project-directory', $ProjectDirectory,
        '--env-file', $envFile,
        '--file', $baseCompose,
        '--file', $windowsCompose,
        '--profile', 'container-control'
    )

    $composeJson = Invoke-ComposeCapture -Arguments @('config', '--format', 'json')
    $composeConfig = $composeJson | ConvertFrom-Json
    if ([string]::IsNullOrWhiteSpace($composeConfig.name)) {
        throw 'Compose did not resolve a project name; refusing to risk a new data volume.'
    }

    $databaseId = (Invoke-ComposeCapture -Arguments @('ps', '--quiet', 'postgres')).Trim()
    if ([string]::IsNullOrWhiteSpace($databaseId)) {
        throw 'The existing PostgreSQL service is not running; refusing to create a new instance.'
    }
    $databaseInspection = & docker inspect --format '{{.State.Health.Status}}|{{json .Config.Labels}}' $databaseId
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($databaseInspection)) {
        throw 'Could not inspect the existing PostgreSQL container.'
    }
    $databaseStateParts = $databaseInspection.Trim() -split '\|', 2
    if ($databaseStateParts.Count -ne 2) {
        throw 'Docker returned incomplete PostgreSQL container state.'
    }
    $databaseLabels = $databaseStateParts[1] | ConvertFrom-Json
    if ($databaseStateParts[0] -ne 'healthy' -or $databaseLabels.'com.docker.compose.project' -ne $composeConfig.name) {
        throw 'The existing PostgreSQL container is unhealthy or belongs to a different Compose project.'
    }

    $healthUri = "http://127.0.0.1:$apiPort/health"
    $existingHealth = Invoke-RestMethod -Uri $healthUri -TimeoutSec 5
    if ($existingHealth.status -ne 'ok') {
        throw 'The existing API did not pass its health check; refusing to deploy over an unhealthy instance.'
    }

    Invoke-Compose -Arguments @('config', '--quiet')
    if ($ValidateOnly) {
        [pscustomobject]@{
            status = 'validated'
            release = $ReleaseVersion
            compose_project = $composeConfig.name
            database = 'healthy and existing'
            api = 'healthy'
            data_volumes = 'preserved'
        } | ConvertTo-Json -Compress
        exit 0
    }

    $backupDirectory = Join-Path $ProjectDirectory '.decagent/backups'
    New-Item -ItemType Directory -Path $backupDirectory -Force | Out-Null
    $stamp = [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssZ')
    $databaseBackupName = "predeploy-$ReleaseVersion-$stamp.dump"
    $databaseBackup = Join-Path $backupDirectory $databaseBackupName
    $temporaryDatabaseBackup = '/tmp/decagent-predeploy-' + [guid]::NewGuid().ToString('N') + '.dump'

    Invoke-Compose -Arguments @(
        'exec', '-T', 'postgres', 'sh', '-ec',
        'export PGPASSWORD="$(cat /run/secrets/postgres_password)"; pg_dump -h 127.0.0.1 -U decagent -d decagent -Fc -f "$1"',
        'sh', $temporaryDatabaseBackup
    )
    Invoke-Compose -Arguments @('cp', "postgres:$temporaryDatabaseBackup", $databaseBackup)
    $backupInfo = Get-Item -LiteralPath $databaseBackup
    if ($backupInfo.Length -le 0) {
        throw 'The PostgreSQL backup is empty; refusing to run migrations.'
    }
    $backupCreated = $true
    try {
        Invoke-Compose -Arguments @('exec', '-T', 'postgres', 'rm', '-f', $temporaryDatabaseBackup)
        $temporaryDatabaseBackup = $null
    } catch {
        # The backup is already copied to the host; a leftover temporary file is harmless.
    }

    docker pull $RunnerImage
    if ($LASTEXITCODE -ne 0) {
        throw 'Could not pull the published runner image.'
    }
    Invoke-Compose -Arguments @('pull', 'migrate', 'api', 'worker', 'egress-proxy', 'executor-broker')
    Invoke-Compose -Arguments @('up', '--detach', '--no-build', '--pull', 'never', '--wait', '--wait-timeout', '180')

    $deadline = [DateTime]::UtcNow.AddMinutes(3)
    $healthy = $false
    while ([DateTime]::UtcNow -lt $deadline) {
        try {
            $health = Invoke-RestMethod -Uri $healthUri -TimeoutSec 5
            if ($health.status -eq 'ok') {
                $healthy = $true
                break
            }
        } catch {
            Start-Sleep -Seconds 5
        }
    }
    if (-not $healthy) {
        throw 'The updated API did not become healthy before the deployment timeout.'
    }

    [pscustomobject]@{
        status = 'deployed'
        release = $ReleaseVersion
        api = 'healthy'
        database_backup = ".decagent/backups/$databaseBackupName"
        compose_project = $composeConfig.name
    } | ConvertTo-Json -Compress
} catch {
    if ($backupCreated) {
        [Console]::Error.WriteLine("Deployment failed. The pre-deployment database backup is retained at .decagent/backups/$([System.IO.Path]::GetFileName($databaseBackup)). Review the migration state before restoring it.")
    } else {
        [Console]::Error.WriteLine('Deployment failed before a database backup was completed; no migration was started.')
    }
    [Console]::Error.WriteLine($_.Exception.Message)
    exit 1
} finally {
    if ($null -ne $temporaryDatabaseBackup -and $null -ne $script:ComposeBaseArguments) {
        try {
            Invoke-Compose -Arguments @('exec', '-T', 'postgres', 'rm', '-f', $temporaryDatabaseBackup)
        } catch {
        }
    }
}
