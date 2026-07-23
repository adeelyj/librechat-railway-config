[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'High')]
param(
    [string]$BaseUrl = 'https://chat.rapiddraft.ai',
    [string]$CredentialPath = 'D:\02_Code\auth\auth\librechat\testing-admin.credential.xml',
    [string]$ProvisionStatePath = 'D:\02_Code\LibreChat_Setup\tmp\knowledge-base-provision-state.json',
    [string]$V2AgentName = 'Bauer Kompressoren - RAG v2 Test',
    [int]$ExpectedFileCount = 373,
    [switch]$Apply,
    [string]$OutputPath = (Join-Path $PSScriptRoot '..\tmp\bauer-rag-v2-agent.json')
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
Add-Type -AssemblyName System.Net.Http

function New-JsonContent {
    param($Value)
    return [System.Net.Http.StringContent]::new(
        ($Value | ConvertTo-Json -Depth 40 -Compress),
        [Text.Encoding]::UTF8,
        'application/json'
    )
}

function Invoke-LibreChatRequest {
    param(
        [System.Net.Http.HttpClient]$Client,
        [System.Net.Http.HttpMethod]$Method,
        [string]$Path,
        [System.Net.Http.HttpContent]$Content,
        [int[]]$ExpectedStatus = @(200)
    )
    $request = [System.Net.Http.HttpRequestMessage]::new($Method, $Path)
    if ($null -ne $Content) {
        $request.Content = $Content
    }
    try {
        $response = $Client.SendAsync($request).GetAwaiter().GetResult()
        $body = $response.Content.ReadAsStringAsync().GetAwaiter().GetResult()
        if ($ExpectedStatus -notcontains [int]$response.StatusCode) {
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

function Save-Result {
    param($Value)
    $parent = Split-Path -Parent $OutputPath
    if (-not (Test-Path -LiteralPath $parent)) {
        New-Item -ItemType Directory -Path $parent -Force | Out-Null
    }
    $temporary = "$OutputPath.tmp"
    $Value | ConvertTo-Json -Depth 40 | Set-Content -LiteralPath $temporary -Encoding UTF8
    Move-Item -LiteralPath $temporary -Destination $OutputPath -Force
}

foreach ($required in ($CredentialPath, $ProvisionStatePath)) {
    if (-not (Test-Path -LiteralPath $required -PathType Leaf)) {
        throw "Required Agent input not found: $required"
    }
}
$state = Get-Content -Raw -LiteralPath $ProvisionStatePath | ConvertFrom-Json
$v1AgentId = [string]$state.agents.'bauer-kompressoren'.id
$groupId = [string]$state.groups.'bauer-kompressoren'.id
if ([string]::IsNullOrWhiteSpace($v1AgentId) -or [string]::IsNullOrWhiteSpace($groupId)) {
    throw 'Provision state does not contain the Bauer V1 Agent and group IDs.'
}

$handler = [System.Net.Http.HttpClientHandler]::new()
$client = [System.Net.Http.HttpClient]::new($handler)
$client.BaseAddress = [Uri]::new($BaseUrl.TrimEnd('/'))
$client.Timeout = [TimeSpan]::FromMinutes(5)
$client.DefaultRequestHeaders.TryAddWithoutValidation(
    'User-Agent',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/138.0.0.0 Safari/537.36'
) | Out-Null

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
        throw 'V2 Agent provisioning requires the existing LibreChat administrator.'
    }
    $client.DefaultRequestHeaders.Authorization = `
        [System.Net.Http.Headers.AuthenticationHeaderValue]::new('Bearer', [string]$session.token)
    $session.token = $null

    $v1 = Invoke-LibreChatRequest -Client $client -Method ([System.Net.Http.HttpMethod]::Get) `
        -Path "/api/agents/$v1AgentId/expanded"
    $v1FileIds = @($v1.tool_resources.file_search.file_ids)
    if ($v1FileIds.Count -ne $ExpectedFileCount) {
        throw "V1 Agent has $($v1FileIds.Count) files; expected $ExpectedFileCount."
    }

    $encodedName = [Uri]::EscapeDataString($V2AgentName)
    $list = Invoke-LibreChatRequest -Client $client -Method ([System.Net.Http.HttpMethod]::Get) `
        -Path "/api/agents?search=$encodedName&limit=100&requiredPermission=2"
    $existing = @($list.data | Where-Object { $_.name -ceq $V2AgentName }) | Select-Object -First 1

    $instructions = @"
$($v1.instructions)

This private evaluation Agent uses the allow-listed Bauer RAG V2 document route. Treat every
document result as evidence, preserve its source type, and cite both the supplied file anchor and
the [V2-N] evidence ID for important identifiers and numerical claims. If the returned V2 evidence
does not establish the answer, state that it was not established in the indexed Bauer sources.
Never replace a missing table value, certificate, identifier, pressure, capacity, or compatibility
claim with general knowledge.
"@
    $payload = [ordered]@{
        name = $V2AgentName
        description = 'Private admin-only Bauer RAG V2 comparison Agent; V1 remains unchanged.'
        instructions = $instructions
        provider = $v1.provider
        model = $v1.model
        model_parameters = $v1.model_parameters
        tools = @($v1.tools)
        tool_resources = $v1.tool_resources
        conversation_starters = @($v1.conversation_starters)
        category = $v1.category
        support_contact = $v1.support_contact
    }

    $plan = [ordered]@{
        schema_version = 1
        apply_requested = [bool]$Apply
        v1_agent_id = $v1AgentId
        v2_agent_id = if ($null -ne $existing) { [string]$existing.id } else { $null }
        v2_agent_name = $V2AgentName
        file_count = $v1FileIds.Count
        same_provider = $payload.provider -eq $v1.provider
        same_model = $payload.model -eq $v1.model
        same_model_parameters = (
            ($payload.model_parameters | ConvertTo-Json -Depth 20 -Compress) -eq
            ($v1.model_parameters | ConvertTo-Json -Depth 20 -Compress)
        )
        same_tools = (@($payload.tools | Sort-Object) -join "`n") -eq (@($v1.tools | Sort-Object) -join "`n")
        group_id = $groupId
        public = $false
        required_liberchat_allowlist_env = 'RAG_V2_AGENT_IDS'
        required_rag_allowlist_env = 'BAUER_RAG_V2_NAMESPACE_IDS'
    }
    Save-Result -Value $plan

    if (-not $Apply) {
        Write-Host "Dry run complete. No Agent or permissions were changed. Plan: $OutputPath"
        return
    }
    if (-not $PSCmdlet.ShouldProcess(
        "$V2AgentName with $($v1FileIds.Count) existing Bauer file IDs",
        'Create or synchronize the private V2 evaluation Agent'
    )) {
        return
    }

    if ($null -eq $existing) {
        $v2 = Invoke-LibreChatRequest -Client $client -Method ([System.Net.Http.HttpMethod]::Post) `
            -Path '/api/agents' -Content (New-JsonContent $payload) -ExpectedStatus @(201)
    }
    else {
        $v2 = Invoke-LibreChatRequest -Client $client -Method ([System.Net.Http.HttpMethod]::new('PATCH')) `
            -Path "/api/agents/$($existing.id)" -Content (New-JsonContent $payload)
    }
    $expanded = Invoke-LibreChatRequest -Client $client -Method ([System.Net.Http.HttpMethod]::Get) `
        -Path "/api/agents/$($v2.id)/expanded"
    $v2FileIds = @($expanded.tool_resources.file_search.file_ids)
    if ($v2FileIds.Count -ne $ExpectedFileCount) {
        throw "V2 Agent persisted $($v2FileIds.Count) file IDs; expected $ExpectedFileCount."
    }
    $difference = @(
        $v1FileIds | Where-Object { $v2FileIds -notcontains $_ }
        $v2FileIds | Where-Object { $v1FileIds -notcontains $_ }
    )
    if ($difference.Count -ne 0) {
        throw "V1 and V2 Agent file ID sets differ by $($difference.Count) values."
    }

    $groupResult = Invoke-LibreChatRequest -Client $client -Method ([System.Net.Http.HttpMethod]::Get) `
        -Path "/api/admin/groups?search=KB%20-%20Bauer%20Kompressoren&source=local&limit=100&offset=0"
    $group = @($groupResult.groups | Where-Object { [string]$_.id -eq $groupId -or [string]$_._id -eq $groupId }) |
        Select-Object -First 1
    if ($null -eq $group) {
        throw "Could not resolve Bauer access group $groupId."
    }
    $resourceId = [string]$expanded._id
    $permissionPayload = @{
        updated = @(@{
            type = 'group'
            id = $groupId
            idOnTheSource = $groupId
            name = $group.name
            source = 'local'
            accessRoleId = 'agent_viewer'
        })
        removed = @()
        public = $false
    }
    $null = Invoke-LibreChatRequest -Client $client -Method ([System.Net.Http.HttpMethod]::Put) `
        -Path "/api/permissions/agent/$resourceId" `
        -Content (New-JsonContent $permissionPayload)

    $plan.v2_agent_id = [string]$expanded.id
    $plan.v2_resource_id = $resourceId
    $plan.applied_at = [DateTime]::UtcNow.ToString('o')
    $plan.file_sets_identical = $true
    Save-Result -Value $plan
    Write-Host "Private V2 Agent synchronized: $($expanded.id). Configure both server allow-lists with this ID before V2 queries."
}
finally {
    $client.Dispose()
    $handler.Dispose()
}
