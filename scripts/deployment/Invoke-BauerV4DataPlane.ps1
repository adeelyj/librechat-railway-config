[CmdletBinding()]
param(
    [ValidateSet('Setup', 'Status', 'RetryDead', 'ResetBuild', 'MarkReady')]
    [string]$Phase = 'Status',
    [string]$IdentifiersPath = (
        'D:\02_Code\LibreChat_Setup-rag-v4-answer-quality\tmp\v4-deploy\' +
        'deployment-identifiers.json'
    ),
    [string]$SecretBundlePath = (
        'D:\02_Code\LibreChat_Setup\tmp\v3-deploy\secrets\' +
        'bauer-v3-shadow-secrets.clixml'
    ),
    [string]$OutputPath = (
        'D:\02_Code\LibreChat_Setup-rag-v4-answer-quality\tmp\v4-deploy\evidence\' +
        'v4-data-plane-status.json'
    ),
    [string]$EvaluationPath,
    [string]$SyntheticSourceManifestPath = (
        'D:\02_Code\LibreChat_Setup-rag-v4-answer-quality\tmp\v4-deploy\' +
        'synthetic-source-stage.json'
    )
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$Railway = (
    'D:\02_Code\LibreChat_Setup\tmp\v3-deploy\railway-cli\' +
    'node_modules\@railway\cli\bin\railway.exe'
)
$Python = (
    'D:\02_Code\LibreChat_Setup\tmp\v3-deploy\venv-api\' +
    'Scripts\python.exe'
)
$Child = Join-Path $PSScriptRoot 'v4_data_plane.py'
$ProjectId = '45bb0e8d-9eca-4973-8026-a3eddbd092b6'
$Environment = 'testing'
$PostgresService = 'bauer-v3-shadow-postgres'

function Get-FreePort {
    $listener = [Net.Sockets.TcpListener]::new(
        [Net.IPAddress]::Loopback, 0
    )
    try {
        $listener.Start()
        return ([Net.IPEndPoint]$listener.LocalEndpoint).Port
    }
    finally { $listener.Stop() }
}

function Start-PrivateTunnel {
    param([int]$Port)
    $start = [Diagnostics.ProcessStartInfo]::new()
    $start.FileName = $Railway
    $start.Arguments = (
        'connect "{0}" --ssh --tunnel-only --port {1} ' +
        '--project "{2}" --environment "{3}"'
    ) -f $PostgresService, $Port, $ProjectId, $Environment
    $start.UseShellExecute = $false
    $start.CreateNoWindow = $true
    $start.RedirectStandardOutput = $true
    $start.RedirectStandardError = $true
    $process = [Diagnostics.Process]::new()
    $process.StartInfo = $start
    if (-not $process.Start()) {
        throw 'Private database tunnel process did not start.'
    }
    $stdoutTask = $process.StandardOutput.ReadToEndAsync()
    $stderrTask = $process.StandardError.ReadToEndAsync()
    $tunnel = [pscustomobject]@{
        Process = $process
        StdOutTask = $stdoutTask
        StdErrTask = $stderrTask
    }
    $deadline = [DateTime]::UtcNow.AddSeconds(45)
    while ([DateTime]::UtcNow -lt $deadline) {
        if ($process.HasExited) {
            $exitCode = $process.ExitCode
            Stop-PrivateTunnel -Tunnel $tunnel
            throw (
                'Private database tunnel exited before readiness ' +
                "(exit $exitCode; output suppressed)."
            )
        }
        $client = [Net.Sockets.TcpClient]::new()
        try {
            if (
                $client.ConnectAsync('127.0.0.1', $Port).Wait(500) -and
                $client.Connected
            ) {
                return $tunnel
            }
        }
        catch {}
        finally { $client.Dispose() }
        Start-Sleep -Milliseconds 250
    }
    Stop-PrivateTunnel -Tunnel $tunnel
    throw 'Private database tunnel did not become ready.'
}

function Stop-PrivateTunnel {
    param($Tunnel)
    if ($null -eq $Tunnel) { return }
    $process = $Tunnel.Process
    try {
        $rootId = $null
        try { $rootId = $process.Id } catch {}
        if ($null -ne $rootId) {
            $all = @(
                Get-CimInstance Win32_Process -ErrorAction SilentlyContinue
            )
            $parents = @([int]$rootId)
            $descendants = [Collections.Generic.List[object]]::new()
            while ($parents.Count -gt 0) {
                $next = @(
                    $all | Where-Object {
                        $parents -contains [int]$_.ParentProcessId
                    }
                )
                if ($next.Count -eq 0) { break }
                foreach ($child in $next) {
                    if (
                        @(
                            $descendants | Where-Object {
                                [int]$_.ProcessId -eq
                                    [int]$child.ProcessId
                            }
                        ).Count -eq 0
                    ) {
                        $descendants.Add($child)
                    }
                }
                $parents = @($next.ProcessId)
            }
            foreach (
                $child in @(
                    $descendants | Sort-Object ProcessId -Descending
                )
            ) {
                Stop-Process -Id ([int]$child.ProcessId) -Force `
                    -ErrorAction SilentlyContinue
            }
        }
        if (-not $process.HasExited) {
            $process.Kill()
            $null = $process.WaitForExit(5000)
        }
        foreach ($task in @($Tunnel.StdOutTask, $Tunnel.StdErrTask)) {
            if ($null -ne $task -and $task.Wait(5000)) {
                $null = $task.Result
            }
        }
    }
    catch {
        # The exact tunnel process tree may already have exited.
    }
    finally { $process.Dispose() }
}

function Convert-SecureString {
    param([Security.SecureString]$Value)
    $pointer = [IntPtr]::Zero
    try {
        $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR(
            $Value
        )
        return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer)
    }
    finally {
        if ($pointer -ne [IntPtr]::Zero) {
            [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer)
        }
    }
}

function Invoke-Child {
    param([string]$InputJson, [string]$Operation)
    $start = [Diagnostics.ProcessStartInfo]::new()
    $start.FileName = $Python
    $start.Arguments = (
        '"{0}" --operation "{1}"' -f $Child, $Operation
    )
    $start.UseShellExecute = $false
    $start.CreateNoWindow = $true
    $start.RedirectStandardInput = $true
    $start.RedirectStandardOutput = $true
    $start.RedirectStandardError = $true
    $process = [Diagnostics.Process]::new()
    $process.StartInfo = $start
    try {
        $null = $process.Start()
        $stdout = $process.StandardOutput.ReadToEndAsync()
        $stderr = $process.StandardError.ReadToEndAsync()
        $process.StandardInput.Write($InputJson)
        $process.StandardInput.Close()
        $process.WaitForExit()
        $raw = $stdout.Result
        $null = $stderr.Result
        if ($process.ExitCode -ne 0) {
            $errorType = 'suppressed'
            $errorFingerprint = 'absent'
            try {
                $failure = $raw | ConvertFrom-Json
                if ($null -ne $failure.PSObject.Properties['error_type']) {
                    $errorType = [string]$failure.error_type
                }
                if (
                    $null -ne
                        $failure.PSObject.Properties['error_fingerprint']
                ) {
                    $errorFingerprint = [string]$failure.error_fingerprint
                }
            }
            catch {}
            throw (
                'V4 data plane failed: {0} ({1})' -f
                $errorType,
                $errorFingerprint
            )
        }
        return $raw | ConvertFrom-Json
    }
    finally { $process.Dispose() }
}

$ids = Get-Content -Raw -LiteralPath $IdentifiersPath | ConvertFrom-Json
$secrets = Import-Clixml -LiteralPath $SecretBundlePath
$evaluation = $null
$syntheticSource = $null
if ($Phase -eq 'MarkReady') {
    if (-not (Test-Path -LiteralPath $EvaluationPath -PathType Leaf)) {
        throw 'MarkReady requires an exact development evaluation file.'
    }
    $evaluation = Get-Content -Raw -LiteralPath $EvaluationPath |
        ConvertFrom-Json
}
if ($Phase -eq 'Setup') {
    if (
        -not (
            Test-Path -LiteralPath $SyntheticSourceManifestPath -PathType Leaf
        )
    ) {
        throw 'Setup requires the verified synthetic source manifest.'
    }
    $syntheticSource = Get-Content -Raw `
        -LiteralPath $SyntheticSourceManifestPath | ConvertFrom-Json
    if (
        -not $syntheticSource.read_after_write_verified -or
        $syntheticSource.credentials_or_connection_details_emitted
    ) {
        throw 'Synthetic source manifest is not safe and verified.'
    }
}
$port = Get-FreePort
$tunnel = $null
$ownerPassword = $null
$requestJson = $null
try {
    $tunnel = Start-PrivateTunnel -Port $port
    $ownerPassword = Convert-SecureString -Value $secrets.postgres_password
    $request = [ordered]@{
        operation = $Phase.ToLowerInvariant().
            Replace('markready', 'mark-ready').
            Replace('retrydead', 'retry-dead').
            Replace('resetbuild', 'reset-build')
        database = 'bauer_v3'
        port = $port
        owner_user = 'postgres'
        owner_password = $ownerPassword
        tenant_id = [string]$ids.tenant_id
        knowledge_base_id = [string]$ids.knowledge_base_id
        principal_id = [string]$ids.principal_id
        release_id = [string]$ids.release_id
        v3_source_release_id = [string]$ids.v3_source_release_id
        release_public_id = [string]$ids.release_public_id
        evaluation = $evaluation
        synthetic_source = $syntheticSource
    }
    $requestJson = $request | ConvertTo-Json -Depth 20 -Compress
    $result = Invoke-Child -InputJson $requestJson -Operation $Phase
    $json = $result | ConvertTo-Json -Depth 30
    if (
        $json -match 'postgres(?:ql)?://' -or
        $json -match '"password"\s*:'
    ) {
        throw 'Refusing to persist possible connection material.'
    }
    $parent = Split-Path -Parent $OutputPath
    $null = New-Item -ItemType Directory -Force -Path $parent
    [IO.File]::WriteAllText(
        $OutputPath,
        $json + [Environment]::NewLine,
        [Text.UTF8Encoding]::new($false)
    )
    [ordered]@{
        phase = $Phase
        output_path = $OutputPath
        sha256 = (Get-FileHash $OutputPath -Algorithm SHA256).
            Hash.ToLowerInvariant()
        credentials_or_connection_details_emitted = $false
    } | ConvertTo-Json -Compress
}
finally {
    Stop-PrivateTunnel -Tunnel $tunnel
    $requestJson = $null
    $request = $null
    $ownerPassword = $null
    $syntheticSource = $null
    $evaluation = $null
    $secrets = $null
    [GC]::Collect()
}
