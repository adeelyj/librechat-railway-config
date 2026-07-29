[CmdletBinding()]
param(
    [string]$OutputPath = (
        'D:\02_Code\LibreChat_Setup-rag-v4\tmp\v4-deploy\evidence\' +
        'wp9-rollback-baseline.json'
    )
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$Railway = (
    'D:\02_Code\LibreChat_Setup\tmp\v3-deploy\railway-cli\' +
    'node_modules\@railway\cli\bin\railway.exe'
)
$ProjectId = '45bb0e8d-9eca-4973-8026-a3eddbd092b6'
$EnvironmentId = '6c80a4d1-c8e3-4410-b712-a27f89012046'
$StatePath = 'D:\02_Code\LibreChat_Setup\tmp\v3-deploy\railway\state.json'
$ServiceIds = [ordered]@{
    librechat = 'c7f709a4-ec29-4ead-b21f-34439cdfb9e3'
    rag_api = 'b8069ba5-2c06-4f90-9a72-f854a596ba42'
    v3_postgres = '3454d164-bbeb-4309-a483-291628171fd2'
    v3_migrator = '7f1f66ea-a994-49b7-8143-0d89bb52ccaf'
    v3_worker = 'b2621053-a240-48c6-abed-6f48f31fe141'
    v3_api = '138c97a8-1f5d-45c9-a5e6-4ea5291e142e'
}

function Get-Sha256Text {
    param([Parameter(Mandatory = $true)][string]$Value)
    $bytes = [Text.Encoding]::UTF8.GetBytes($Value)
    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        return ([BitConverter]::ToString($sha.ComputeHash($bytes))).
            Replace('-', '').ToLowerInvariant()
    }
    finally {
        [Array]::Clear($bytes, 0, $bytes.Length)
        $sha.Dispose()
    }
}

function Invoke-CapturedRailway {
    param([Parameter(Mandatory = $true)][string[]]$Arguments)
    $start = [Diagnostics.ProcessStartInfo]::new()
    $start.FileName = $Railway
    $start.WorkingDirectory = 'D:\02_Code\LibreChat_Setup'
    $start.Arguments = ($Arguments | ForEach-Object {
        '"' + ([string]$_).Replace('"', '\"') + '"'
    }) -join ' '
    $start.UseShellExecute = $false
    $start.CreateNoWindow = $true
    $start.RedirectStandardOutput = $true
    $start.RedirectStandardError = $true
    $process = [Diagnostics.Process]::new()
    $process.StartInfo = $start
    try {
        $null = $process.Start()
        $stdout = $process.StandardOutput.ReadToEndAsync()
        $stderr = $process.StandardError.ReadToEndAsync()
        $process.WaitForExit()
        $raw = $stdout.Result
        $null = $stderr.Result
        if ($process.ExitCode -ne 0) {
            $raw = $null
            throw 'A Railway baseline read failed; diagnostics were suppressed.'
        }
        return $raw
    }
    finally {
        $process.Dispose()
    }
}

$statusRaw = $null
$variablesRaw = $null
try {
    $statusRaw = Invoke-CapturedRailway -Arguments @('status', '--json')
    $status = $statusRaw | ConvertFrom-Json
    if ([string]$status.id -cne $ProjectId) {
        throw 'Railway baseline project mismatch.'
    }
    $environment = @(
        $status.environments.edges.node |
            Where-Object { [string]$_.id -ceq $EnvironmentId }
    )
    if ($environment.Count -ne 1) {
        throw 'Railway baseline environment mismatch.'
    }
    $instances = @($environment[0].serviceInstances.edges.node)
    $services = [ordered]@{}
    $variableFingerprints = [ordered]@{}
    foreach ($entry in $ServiceIds.GetEnumerator()) {
        $instance = @(
            $instances |
                Where-Object { [string]$_.serviceId -ceq [string]$entry.Value }
        )
        if ($instance.Count -ne 1) {
            throw "Baseline service identity missing: $($entry.Key)"
        }
        $deployment = $instance[0].latestDeployment
        $meta = $deployment.meta
        $commit = if ($null -ne $meta.PSObject.Properties['commitHash']) {
            [string]$meta.commitHash
        }
        else { '' }
        $rootDirectory = if (
            $null -ne $meta.PSObject.Properties['rootDirectory']
        ) { [string]$meta.rootDirectory } else { '' }
        $configFile = if ($null -ne $meta.PSObject.Properties['configFile']) {
            [string]$meta.configFile
        }
        else { '' }
        $services[$entry.Key] = [ordered]@{
            service_id = [string]$entry.Value
            service_name = [string]$instance[0].serviceName
            deployment_id = [string]$deployment.id
            deployment_status = [string]$deployment.status
            deployment_commit = $commit
            root_directory = $rootDirectory
            config_file = $configFile
            replica_count = @($deployment.instances).Count
        }
        $variablesRaw = Invoke-CapturedRailway -Arguments @(
            'variable', 'list',
            '--service', [string]$entry.Value,
            '--environment', $EnvironmentId,
            '--project', $ProjectId,
            '--json'
        )
        $variables = $variablesRaw | ConvertFrom-Json
        $pairs = @(
            $variables.PSObject.Properties |
                Sort-Object Name |
                ForEach-Object {
                    $_.Name + '=' + (Get-Sha256Text -Value ([string]$_.Value))
                }
        )
        $variableFingerprints[$entry.Key] = [ordered]@{
            variable_count = $pairs.Count
            names = @($variables.PSObject.Properties.Name | Sort-Object)
            values_sha256 = Get-Sha256Text -Value ($pairs -join "`n")
        }
        $variablesRaw = $null
        $variables = $null
        $pairs = $null
    }
    $state = Get-Content -Raw -LiteralPath $StatePath | ConvertFrom-Json
    $document = [ordered]@{
        schema_version = 'bauer-rag-v4-wp9-rollback-baseline/v1'
        captured_at_utc = [DateTime]::UtcNow.ToString('o')
        project_id = $ProjectId
        environment_id = $EnvironmentId
        services = $services
        variable_fingerprints = $variableFingerprints
        protected_agents = [ordered]@{
            v1 = 'agent_Z8A2LtQWLeP4KuUDbvqZL'
            v2 = 'agent_pmPMcA25UXS7vznUaz-DU'
            v3 = 'agent_DzeT_ugU3tuZC_VCKB8Bh'
        }
        v3_release = [ordered]@{
            candidate_release_id = [string]$state.checkpoints.verification.fixed_candidate_release_id
            migration_version = 20
            active_release_count = 0
            active_release_pointer_action = 'never_requested_or_mutated'
        }
        rollback = [ordered]@{
            restore_deployment_ids = [ordered]@{
                migrator = [string]$services.v3_migrator.deployment_id
                worker = [string]$services.v3_worker.deployment_id
                api = [string]$services.v3_api.deployment_id
                librechat = [string]$services.librechat.deployment_id
            }
            restore_settings_from_variable_fingerprints = $true
            drop_only_v4_schema_roles_and_v4_objects = $true
        }
        locked_holdout_opened = $false
        active_release_pointer_mutation_authorized = $false
        credentials_or_tokens_emitted = $false
    }
    $json = $document | ConvertTo-Json -Depth 30
    if (
        $json -match 'postgres(?:ql)?://' -or
        $json -match '"password"\s*:' -or
        $json -match '"token"\s*:'
    ) {
        throw 'Refusing to persist possible secret material.'
    }
    $parent = Split-Path -Parent $OutputPath
    $null = New-Item -ItemType Directory -Path $parent -Force
    [IO.File]::WriteAllText(
        $OutputPath,
        $json + [Environment]::NewLine,
        [Text.UTF8Encoding]::new($false)
    )
    [ordered]@{
        output_path = $OutputPath
        sha256 = (Get-FileHash -LiteralPath $OutputPath -Algorithm SHA256).
            Hash.ToLowerInvariant()
        service_count = $services.Count
        credentials_or_tokens_emitted = $false
    } | ConvertTo-Json -Compress
}
finally {
    $statusRaw = $null
    $variablesRaw = $null
    [GC]::Collect()
}
