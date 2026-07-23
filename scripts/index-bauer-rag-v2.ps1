[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'High')]
param(
    [string]$RagApiUrl = 'https://rag-api-testing.up.railway.app',
    [string]$LibreChatUrl = 'https://chat.rapiddraft.ai',
    [string]$CredentialPath = 'D:\02_Code\auth\auth\librechat\testing-admin.credential.xml',
    [string]$CorpusPath = 'D:\02_Code\LibreChat_Setup\tmp\corpora\bauer-kompressoren',
    [string]$ManifestPath = (Join-Path $PSScriptRoot '..\evals\bauer-rag-v2\baselines\corpus-manifest.json'),
    [string]$Namespace,
    [string]$V2AgentStatePath = (Join-Path $PSScriptRoot '..\tmp\bauer-rag-v2-agent.json'),
    [string]$IndexVersion = 'bauer-rag-v2-2026-07-r2',
    [string]$EmbeddingVersion = 'local/embed-engineering-1024',
    [Guid]$ResumeRunId,
    [switch]$Activate,
    [int]$ProgressInterval = 10,
    [string]$StatePath = (Join-Path $PSScriptRoot '..\tmp\bauer-rag-v2-index-state.json')
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
Add-Type -AssemblyName System.Net.Http

function New-JsonContent {
    param($Value)
    return [System.Net.Http.StringContent]::new(
        ($Value | ConvertTo-Json -Depth 30 -Compress),
        [Text.Encoding]::UTF8,
        'application/json'
    )
}

function Invoke-JsonRequest {
    param(
        [System.Net.Http.HttpClient]$Client,
        [System.Net.Http.HttpMethod]$Method,
        [string]$Path,
        [System.Net.Http.HttpContent]$Content
    )
    $request = [System.Net.Http.HttpRequestMessage]::new($Method, $Path)
    if ($null -ne $Content) {
        $request.Content = $Content
    }
    try {
        $response = $Client.SendAsync($request).GetAwaiter().GetResult()
        $body = $response.Content.ReadAsStringAsync().GetAwaiter().GetResult()
        if (-not $response.IsSuccessStatusCode) {
            throw "HTTP $([int]$response.StatusCode) from $Path."
        }
        if ([string]::IsNullOrWhiteSpace($body)) {
            return $null
        }
        return $body | ConvertFrom-Json
    }
    finally {
        $request.Dispose()
    }
}

function Save-IndexState {
    param($Value)
    $parent = Split-Path -Parent $StatePath
    if (-not (Test-Path -LiteralPath $parent)) {
        New-Item -ItemType Directory -Path $parent -Force | Out-Null
    }
    $temporary = "$StatePath.tmp"
    $Value | ConvertTo-Json -Depth 20 | Set-Content -LiteralPath $temporary -Encoding UTF8
    Move-Item -LiteralPath $temporary -Destination $StatePath -Force
}

foreach ($required in ($CredentialPath, $CorpusPath, $ManifestPath)) {
    if (-not (Test-Path -LiteralPath $required)) {
        throw "Required index input not found: $required"
    }
}

$manifest = Get-Content -Raw -LiteralPath $ManifestPath | ConvertFrom-Json
$records = @($manifest.records)
if ($records.Count -eq 0 -or $records.Count -gt 1000) {
    throw "Manifest contains an invalid record count: $($records.Count)."
}
$baselineNamespace = [string]$manifest.authorization_namespace
if ([string]::IsNullOrWhiteSpace($baselineNamespace)) {
    throw 'Manifest does not identify its baseline authorization namespace.'
}
if ([string]::IsNullOrWhiteSpace($Namespace) -and (Test-Path -LiteralPath $V2AgentStatePath -PathType Leaf)) {
    $agentState = Get-Content -Raw -LiteralPath $V2AgentStatePath | ConvertFrom-Json
    $Namespace = [string]$agentState.v2_agent_id
}
if ([string]::IsNullOrWhiteSpace($Namespace)) {
    throw 'Supply -Namespace with the private V2 Agent ID, or provision its state file first.'
}
if ($Namespace -eq $baselineNamespace) {
    throw 'Refusing to index V2 under the normal Bauer V1 Agent namespace.'
}
$namespace = $Namespace

$handler = [System.Net.Http.HttpClientHandler]::new()
$handler.AutomaticDecompression = [System.Net.DecompressionMethods]::GZip -bor `
    [System.Net.DecompressionMethods]::Deflate
$libreChat = [System.Net.Http.HttpClient]::new($handler)
$libreChat.BaseAddress = [Uri]::new($LibreChatUrl.TrimEnd('/'))
$libreChat.Timeout = [TimeSpan]::FromMinutes(3)
$libreChat.DefaultRequestHeaders.TryAddWithoutValidation(
    'User-Agent',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36'
) | Out-Null
$rag = [System.Net.Http.HttpClient]::new()
$rag.BaseAddress = [Uri]::new($RagApiUrl.TrimEnd('/'))
$rag.Timeout = [TimeSpan]::FromMinutes(15)

try {
    $credential = Import-Clixml -LiteralPath $CredentialPath
    $plain = $credential.GetNetworkCredential()
    try {
        $session = Invoke-JsonRequest -Client $libreChat -Method ([System.Net.Http.HttpMethod]::Post) `
            -Path '/api/auth/login' `
            -Content (New-JsonContent @{ email = $plain.UserName; password = $plain.Password })
    }
    finally {
        $plain = $null
    }
    if ($session.user.role -ne 'ADMIN') {
        throw 'V2 indexing requires the existing LibreChat administrator.'
    }
    $rag.DefaultRequestHeaders.Authorization = `
        [System.Net.Http.Headers.AuthenticationHeaderValue]::new('Bearer', [string]$session.token)
    $session.token = $null

    if (
        $PSBoundParameters.ContainsKey('ResumeRunId') -and
        $ResumeRunId -ne [Guid]::Empty
    ) {
        $runId = $ResumeRunId
        $run = Invoke-JsonRequest -Client $rag -Method ([System.Net.Http.HttpMethod]::Get) `
            -Path "/v2/index-runs/$runId"
        if ($run.status -ne 'running') {
            throw "Run $runId is $($run.status), not resumable."
        }
        if ([string]$run.namespace -ne $namespace -or [string]$run.index_version -ne $IndexVersion) {
            throw "Run $runId does not match namespace '$namespace' and index version '$IndexVersion'."
        }
    }
    else {
        if (-not $PSCmdlet.ShouldProcess(
            "$RagApiUrl namespace $namespace",
            "Create a staged Bauer RAG V2 index run for $($records.Count) documents"
        )) {
            return
        }
        $run = Invoke-JsonRequest -Client $rag -Method ([System.Net.Http.HttpMethod]::Post) `
            -Path '/v2/index-runs' `
            -Content (New-JsonContent @{
                namespace = $namespace
                index_version = $IndexVersion
                embedding_version = $EmbeddingVersion
                expected_file_count = $records.Count
                metadata = @{
                    manifest_sha256 = (Get-FileHash -LiteralPath $ManifestPath -Algorithm SHA256).Hash.ToLowerInvariant()
                    runner = 'scripts/index-bauer-rag-v2.ps1'
                    baseline_namespace = $baselineNamespace
                    v2_namespace = $namespace
                }
            })
        $runId = [Guid]$run.run_id
    }

    $state = [ordered]@{
        schema_version = 1
        run_id = [string]$runId
        namespace = $namespace
        index_version = $IndexVersion
        started_at = [DateTime]::UtcNow.ToString('o')
        completed = [pscustomobject]@{}
    }
    if (Test-Path -LiteralPath $StatePath -PathType Leaf) {
        $existing = Get-Content -Raw -LiteralPath $StatePath | ConvertFrom-Json
        if ([string]$existing.run_id -eq [string]$runId) {
            $state = $existing
        }
    }

    $completed = 0
    foreach ($record in $records) {
        $filePath = Join-Path $CorpusPath ([string]$record.filename)
        if (-not (Test-Path -LiteralPath $filePath -PathType Leaf)) {
            throw "Corpus file is missing: $filePath"
        }
        $actual = (Get-FileHash -LiteralPath $filePath -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($actual -ne [string]$record.checksum) {
            throw "Checksum mismatch before upload: $($record.filename)"
        }

        $multipart = [System.Net.Http.MultipartFormDataContent]::new()
        try {
            $multipart.Add([System.Net.Http.StringContent]::new([string]$record.file_id), 'file_id')
            $multipart.Add([System.Net.Http.StringContent]::new($actual), 'checksum')
            $multipart.Add([System.Net.Http.StringContent]::new([string]$record.filename), 'filename')
            $multipart.Add([System.Net.Http.StringContent]::new('public_document'), 'source_type')
            $bytes = [IO.File]::ReadAllBytes($filePath)
            $fileContent = [System.Net.Http.ByteArrayContent]::new($bytes)
            $fileContent.Headers.ContentType = `
                [System.Net.Http.Headers.MediaTypeHeaderValue]::new('text/markdown')
            $multipart.Add($fileContent, 'file', [string]$record.filename)
            $result = Invoke-JsonRequest -Client $rag -Method ([System.Net.Http.HttpMethod]::Post) `
                -Path "/v2/index-runs/$runId/documents" `
                -Content $multipart
        }
        finally {
            $multipart.Dispose()
        }
        $state.completed | Add-Member -NotePropertyName ([string]$record.file_id) `
            -NotePropertyValue ([ordered]@{
                action = $result.action
                filename = [string]$record.filename
                completed_at = [DateTime]::UtcNow.ToString('o')
            }) -Force
        $completed++
        if (($completed % $ProgressInterval -eq 0) -or ($completed -eq $records.Count)) {
            Save-IndexState -Value $state
            Write-Host "Staged $completed/$($records.Count) Bauer V2 documents."
        }
    }

    if ($Activate) {
        if (-not $PSCmdlet.ShouldProcess(
            "V2 index run $runId",
            'Atomically activate the complete staged index'
        )) {
            Write-Host "Run $runId is fully staged but remains inactive."
            return
        }
        $activated = Invoke-JsonRequest -Client $rag -Method ([System.Net.Http.HttpMethod]::Post) `
            -Path "/v2/index-runs/$runId/activate"
        $state.status = $activated.status
        $state.activated_at = [DateTime]::UtcNow.ToString('o')
        Save-IndexState -Value $state
        Write-Host "Activated Bauer RAG V2 run $runId with $($activated.file_count) files."
    }
    else {
        $state.status = 'staged_inactive'
        $state.staged_at = [DateTime]::UtcNow.ToString('o')
        Save-IndexState -Value $state
        Write-Host "Run $runId is fully staged and inactive. Re-run with -ResumeRunId $runId -Activate after review."
    }
}
finally {
    $libreChat.Dispose()
    $rag.Dispose()
    $handler.Dispose()
}
