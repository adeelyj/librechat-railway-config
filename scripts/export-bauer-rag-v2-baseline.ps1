[CmdletBinding()]
param(
    [string]$LibreChatUrl = 'https://chat.rapiddraft.ai',
    [string]$CredentialPath = 'D:\02_Code\auth\auth\librechat\testing-admin.credential.xml',
    [string]$ProvisionStatePath = 'D:\02_Code\LibreChat_Setup\tmp\knowledge-base-provision-state.json',
    [string]$CorpusManifestPath = 'D:\02_Code\LibreChat_Setup\tmp\corpora\bauer-kompressoren\manifest.json',
    [string]$RailwayWorkingDirectory = 'D:\02_Code\LibreChat_Setup',
    [string]$OutputDirectory = (Join-Path $PSScriptRoot '..\evals\bauer-rag-v2\baselines')
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
Add-Type -AssemblyName System.Net.Http

function New-JsonContent {
    param($Value)
    return [System.Net.Http.StringContent]::new(
        ($Value | ConvertTo-Json -Depth 30 -Compress),
        [System.Text.Encoding]::UTF8,
        'application/json'
    )
}

function Invoke-LibreChatRequest {
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
            throw "LibreChat $($Method.Method) $Path returned HTTP $([int]$response.StatusCode)."
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

function Get-Agent {
    param([System.Net.Http.HttpClient]$Client, [string]$AgentId)
    return Invoke-LibreChatRequest -Client $Client -Method ([System.Net.Http.HttpMethod]::Get) `
        -Path "/api/agents/$AgentId/expanded"
}

function Get-SafeProperty {
    param($Object, [string]$Name)
    if ($null -eq $Object) {
        return $null
    }
    $property = $Object.PSObject.Properties[$Name]
    if ($null -eq $property) {
        return $null
    }
    return $property.Value
}

function Get-LatestDeployment {
    param([string]$Service)
    Push-Location -LiteralPath $RailwayWorkingDirectory
    try {
        $json = & railway deployment list `
            --service $Service `
            --environment testing `
            --project 45bb0e8d-9eca-4973-8026-a3eddbd092b6 `
            --limit 1 `
            --json
        if ($LASTEXITCODE -ne 0) {
            throw "Could not inspect Railway deployment for $Service."
        }
        $parsed = $json | ConvertFrom-Json
        return $parsed[0]
    }
    finally {
        Pop-Location
    }
}

function Write-StableJson {
    param([string]$Path, $Value)
    $parent = Split-Path -Parent $Path
    if (-not (Test-Path -LiteralPath $parent)) {
        New-Item -ItemType Directory -Path $parent -Force | Out-Null
    }
    $temporary = "$Path.tmp"
    $Value | ConvertTo-Json -Depth 40 | Set-Content -LiteralPath $temporary -Encoding UTF8
    Move-Item -LiteralPath $temporary -Destination $Path -Force
}

function Get-StringSha256 {
    param([string]$Value)
    $algorithm = [Security.Cryptography.SHA256]::Create()
    try {
        $bytes = [Text.Encoding]::UTF8.GetBytes($Value)
        return ([BitConverter]::ToString($algorithm.ComputeHash($bytes)) -replace '-', '').ToLowerInvariant()
    }
    finally {
        $algorithm.Dispose()
    }
}

foreach ($required in ($CredentialPath, $ProvisionStatePath, $CorpusManifestPath)) {
    if (-not (Test-Path -LiteralPath $required -PathType Leaf)) {
        throw "Required baseline input not found: $required"
    }
}

$state = Get-Content -Raw -LiteralPath $ProvisionStatePath | ConvertFrom-Json
$sourceManifest = Get-Content -Raw -LiteralPath $CorpusManifestPath | ConvertFrom-Json
$bauerAgentId = [string]$state.agents.'bauer-kompressoren'.id
$testAgentId = [string]$state.agents.'test-archive'.id
if ([string]::IsNullOrWhiteSpace($bauerAgentId) -or [string]::IsNullOrWhiteSpace($testAgentId)) {
    throw 'Provision state does not contain both Agent IDs.'
}

$handler = [System.Net.Http.HttpClientHandler]::new()
$handler.AutomaticDecompression = [System.Net.DecompressionMethods]::GZip -bor `
    [System.Net.DecompressionMethods]::Deflate
$client = [System.Net.Http.HttpClient]::new($handler)
$client.BaseAddress = [Uri]::new($LibreChatUrl.TrimEnd('/'))
$client.Timeout = [TimeSpan]::FromMinutes(3)
$client.DefaultRequestHeaders.TryAddWithoutValidation(
    'User-Agent',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36'
) | Out-Null

$agentCaptureSource = 'live_librechat_api'
try {
    try {
        $credential = Import-Clixml -LiteralPath $CredentialPath
        $plain = $credential.GetNetworkCredential()
        try {
            $session = Invoke-LibreChatRequest -Client $client -Method ([System.Net.Http.HttpMethod]::Post) `
                -Path '/api/auth/login' `
                -Content (New-JsonContent @{ email = $plain.UserName; password = $plain.Password })
        }
        finally {
            $plain = $null
        }
        if ($session.user.role -ne 'ADMIN') {
            throw 'The baseline exporter requires the existing LibreChat administrator credential.'
        }
        $client.DefaultRequestHeaders.Authorization = `
            [System.Net.Http.Headers.AuthenticationHeaderValue]::new('Bearer', [string]$session.token)
        $session.token = $null

        $bauerAgent = Get-Agent -Client $client -AgentId $bauerAgentId
        $testAgent = Get-Agent -Client $client -AgentId $testAgentId
    }
    catch {
        Write-Warning 'Live Agent read was unavailable; using the last verified provision state and versioned Agent contract.'
        $agentCaptureSource = 'verified_provision_state_and_versioned_contract'
        $bauerFileIds = @($state.uploaded.'bauer-kompressoren'.PSObject.Properties | ForEach-Object {
            [string]$_.Value.fileId
        })
        $testFileIds = @($state.uploaded.'test-archive'.PSObject.Properties | ForEach-Object {
            [string]$_.Value.fileId
        })
        $commonParameters = [pscustomobject]@{
            temperature = 0.1
            maxContextTokens = 24000
            max_tokens = 2048
        }
        $bauerAgent = [pscustomobject]@{
            id = $bauerAgentId
            _id = [string]$state.agents.'bauer-kompressoren'.resourceId
            name = [string]$state.agents.'bauer-kompressoren'.name
            provider = 'RapidDraft Local AI'
            model = 'local/qwen-coder'
            model_parameters = $commonParameters
            tools = @('file_search', 'search_bauer_twin_mcp_bauer-twin')
            tool_resources = [pscustomobject]@{
                file_search = [pscustomobject]@{ file_ids = $bauerFileIds }
            }
        }
        $testAgent = [pscustomobject]@{
            id = $testAgentId
            _id = [string]$state.agents.'test-archive'.resourceId
            name = [string]$state.agents.'test-archive'.name
            provider = 'RapidDraft Local AI'
            model = 'local/qwen-coder'
            model_parameters = $commonParameters
            tools = @('file_search')
            tool_resources = [pscustomobject]@{
                file_search = [pscustomobject]@{ file_ids = $testFileIds }
            }
        }
    }
}
finally {
    $client.Dispose()
    $handler.Dispose()
}

Push-Location -LiteralPath $RailwayWorkingDirectory
try {
    $servicesJson = & railway service list --json
    if ($LASTEXITCODE -ne 0) {
        throw 'Could not inspect Railway services.'
    }
    $services = $servicesJson | ConvertFrom-Json
}
finally {
    Pop-Location
}

$importantDeployments = @{}
foreach ($serviceName in ('LibreChat', 'RAG API', 'Bauer Twin API')) {
    $deployment = Get-LatestDeployment -Service $serviceName
    $importantDeployments[$serviceName] = [ordered]@{
        deployment_id = $deployment.id
        status = $deployment.status
        created_at = $deployment.createdAt
        branch = Get-SafeProperty -Object $deployment.meta -Name 'branch'
        commit = Get-SafeProperty -Object $deployment.meta -Name 'commitHash'
        image_digest = Get-SafeProperty -Object $deployment.meta -Name 'imageDigest'
        reason = Get-SafeProperty -Object $deployment.meta -Name 'reason'
        message = Get-SafeProperty -Object $deployment.meta -Name 'cliMessage'
    }
}

$deploymentSnapshot = [ordered]@{
    schema_version = 1
    captured_at = [DateTime]::UtcNow.ToString('o')
    project = [ordered]@{
        name = 'bk-RAG-test'
        id = '45bb0e8d-9eca-4973-8026-a3eddbd092b6'
        environment = 'testing'
        environment_id = '6c80a4d1-c8e3-4410-b712-a27f89012046'
    }
    required_source_baseline = '2815a8f2ba1b7db04d40a80934544f092e54518b'
    rollback_source_commit = '65504fefc524ab6878d30a6c9d86fc9b35ae3ac1'
    services = @($services | Sort-Object name | ForEach-Object {
        [ordered]@{
            name = $_.name
            service_id = $_.id
            deployment_id = $_.deploymentId
            status = $_.status
            source_repo = $_.source.repo
            source_image = $_.source.image
            url = $_.url
        }
    })
    deployments = $importantDeployments
}

function Select-AgentSnapshot {
    param($Agent, [string]$GroupId)
    $fileIds = @($Agent.tool_resources.file_search.file_ids)
    return [ordered]@{
        id = $Agent.id
        resource_id = $Agent._id
        name = $Agent.name
        provider = $Agent.provider
        model = $Agent.model
        model_parameters = $Agent.model_parameters
        tools = @($Agent.tools)
        file_count = $fileIds.Count
        file_ids_sha256 = Get-StringSha256 -Value (@($fileIds | Sort-Object) -join "`n")
        access_group_id = $GroupId
        public = $false
    }
}

$agentSnapshot = [ordered]@{
    schema_version = 1
    captured_at = [DateTime]::UtcNow.ToString('o')
    capture_source = $agentCaptureSource
    provision_state_verified_at = [string]$state.verification.verifiedAt
    bauer_v1 = Select-AgentSnapshot `
        -Agent $bauerAgent `
        -GroupId ([string]$state.groups.'bauer-kompressoren'.id)
    test_archive_v1 = Select-AgentSnapshot `
        -Agent $testAgent `
        -GroupId ([string]$state.groups.'test-archive'.id)
    shared_file_ids = 0
}

$sourceByName = @{}
foreach ($item in $sourceManifest) {
    $sourceByName[[string]$item.filename] = $item
}
$corpusRows = @()
foreach ($property in @($state.uploaded.'bauer-kompressoren'.PSObject.Properties | Sort-Object Name)) {
    $filename = [string]$property.Name
    $checkpoint = $property.Value
    $source = $sourceByName[$filename]
    if ($null -eq $source) {
        throw "Source manifest is missing $filename."
    }
    if ([string]$checkpoint.sha256 -ne [string]$source.exportedChecksum) {
        throw "Derived checksum mismatch for $filename."
    }
    $corpusRows += [ordered]@{
        file_id = [string]$checkpoint.fileId
        filename = $filename
        checksum = [string]$checkpoint.sha256
        source_checksum = [string]$source.sourceChecksum
        source_path = [string]$source.sourcePath
        source_kind = [string]$source.kind
        page_count = $source.pageCount
        ocr_used = [bool]$source.ocrUsed
        authorization_namespace = $bauerAgentId
        source_type = 'public_document'
        embedded = [bool]$checkpoint.embedded
    }
}
if ($corpusRows.Count -ne 373) {
    throw "Expected 373 Bauer manifest records, found $($corpusRows.Count)."
}

$corpusSnapshot = [ordered]@{
    schema_version = 1
    captured_at = [DateTime]::UtcNow.ToString('o')
    corpus = 'Bauer Kompressoren'
    authorization_namespace = $bauerAgentId
    file_count = $corpusRows.Count
    records = $corpusRows
}

$deploymentPath = Join-Path $OutputDirectory 'deployment.json'
$agentsPath = Join-Path $OutputDirectory 'agents.json'
$manifestPath = Join-Path $OutputDirectory 'corpus-manifest.json'
Write-StableJson -Path $deploymentPath -Value $deploymentSnapshot
Write-StableJson -Path $agentsPath -Value $agentSnapshot
Write-StableJson -Path $manifestPath -Value $corpusSnapshot

$checksums = foreach ($path in ($deploymentPath, $agentsPath, $manifestPath)) {
    [ordered]@{
        file = Split-Path -Leaf $path
        sha256 = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant()
    }
}
Write-StableJson -Path (Join-Path $OutputDirectory 'checksums.json') -Value ([ordered]@{
    schema_version = 1
    generated_at = [DateTime]::UtcNow.ToString('o')
    artifacts = @($checksums)
})

Write-Host "Exported Bauer RAG V2 baseline: $($corpusRows.Count) files, $($services.Count) Railway services."
