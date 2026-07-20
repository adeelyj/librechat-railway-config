[CmdletBinding()]
param(
    [string]$BaseUrl = 'https://librechat-testing.up.railway.app',
    [string]$CredentialPath = 'D:\02_Code\auth\auth\librechat\testing-admin.credential.xml',
    [string]$TestArchivePath = (Join-Path $PSScriptRoot '..\tmp\corpora\test-archive'),
    [string]$BauerPath = (Join-Path $PSScriptRoot '..\tmp\corpora\bauer-kompressoren'),
    [string]$StatePath = (Join-Path $PSScriptRoot '..\tmp\knowledge-base-provision-state.json'),
    [switch]$SkipUploads,
    [switch]$RunQueryTests,
    [int]$ProgressInterval = 10
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

Add-Type -AssemblyName System.Net.Http

function ConvertTo-Hashtable {
    param([Parameter(ValueFromPipeline = $true)]$InputObject)

    if ($null -eq $InputObject) {
        return $null
    }
    if ($InputObject -is [System.Collections.IDictionary]) {
        $table = @{}
        foreach ($key in $InputObject.Keys) {
            $table[$key] = ConvertTo-Hashtable $InputObject[$key]
        }
        return $table
    }
    if ($InputObject -is [System.Management.Automation.PSCustomObject]) {
        $table = @{}
        foreach ($property in $InputObject.PSObject.Properties) {
            $table[$property.Name] = ConvertTo-Hashtable $property.Value
        }
        return $table
    }
    if (($InputObject -is [System.Collections.IEnumerable]) -and -not ($InputObject -is [string])) {
        return @($InputObject | ForEach-Object { ConvertTo-Hashtable $_ })
    }
    return $InputObject
}

function Save-State {
    param([hashtable]$State)

    $parent = Split-Path -Parent $StatePath
    if (-not (Test-Path -LiteralPath $parent)) {
        New-Item -ItemType Directory -Path $parent -Force | Out-Null
    }
    $temporaryPath = "$StatePath.tmp"
    $State.updatedAt = [DateTime]::UtcNow.ToString('o')
    $State | ConvertTo-Json -Depth 20 | Set-Content -LiteralPath $temporaryPath -Encoding UTF8
    Move-Item -LiteralPath $temporaryPath -Destination $StatePath -Force
}

function New-JsonContent {
    param($Value)

    $json = $Value | ConvertTo-Json -Depth 20 -Compress
    return [System.Net.Http.StringContent]::new(
        $json,
        [System.Text.Encoding]::UTF8,
        'application/json'
    )
}

function Invoke-LibreChatRequest {
    param(
        [Parameter(Mandatory = $true)][System.Net.Http.HttpClient]$Client,
        [Parameter(Mandatory = $true)][System.Net.Http.HttpMethod]$Method,
        [Parameter(Mandatory = $true)][string]$Path,
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
        $status = [int]$response.StatusCode
        if ($ExpectedStatus -notcontains $status) {
            $safeBody = if ($body.Length -gt 1000) { $body.Substring(0, 1000) } else { $body }
            throw "LibreChat $($Method.Method) $Path returned HTTP $status. Response: $safeBody"
        }
        if ([string]::IsNullOrWhiteSpace($body)) {
            return $null
        }
        $contentTypeHeader = $response.Content.Headers.ContentType
        $mediaType = if ($null -ne $contentTypeHeader) { $contentTypeHeader.MediaType } else { '' }
        if ($mediaType -eq 'application/json' -or $body.TrimStart().StartsWith('{') -or `
            $body.TrimStart().StartsWith('[')) {
            return $body | ConvertFrom-Json
        }
        return $body
    }
    finally {
        $request.Dispose()
    }
}

function Get-PropertyValue {
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

function Get-OrCreateGroup {
    param(
        [System.Net.Http.HttpClient]$Client,
        [string]$Name,
        [string]$Description,
        [string]$AdminUserId
    )

    $encodedName = [Uri]::EscapeDataString($Name)
    $result = Invoke-LibreChatRequest -Client $Client -Method ([System.Net.Http.HttpMethod]::Get) `
        -Path "/api/admin/groups?search=$encodedName&source=local&limit=100&offset=0"
    $existing = @($result.groups | Where-Object { $_.name -ceq $Name }) | Select-Object -First 1
    if ($null -ne $existing) {
        Write-Host "Reusing access group: $Name"
        return $existing
    }

    $payload = @{
        name = $Name
        description = $Description
        source = 'local'
        memberIds = @($AdminUserId)
    }
    $created = Invoke-LibreChatRequest -Client $Client -Method ([System.Net.Http.HttpMethod]::Post) `
        -Path '/api/admin/groups' -Content (New-JsonContent $payload) -ExpectedStatus @(201)
    Write-Host "Created access group: $Name"
    return $created.group
}

function Get-OrCreateAgent {
    param(
        [System.Net.Http.HttpClient]$Client,
        [string]$Name,
        [string]$Description,
        [string]$Instructions,
        [string[]]$ConversationStarters,
        [string[]]$Tools = @('file_search')
    )

    $encodedName = [Uri]::EscapeDataString($Name)
    $list = Invoke-LibreChatRequest -Client $Client -Method ([System.Net.Http.HttpMethod]::Get) `
        -Path "/api/agents?search=$encodedName&limit=100&requiredPermission=2"
    $listData = Get-PropertyValue -Object $list -Name 'data'
    if ($null -eq $listData) {
        $listData = @()
    }
    $existing = @($listData | Where-Object { $_.name -ceq $Name }) | Select-Object -First 1

    $payload = @{
        name = $Name
        description = $Description
        instructions = $Instructions
        provider = 'RapidDraft Local AI'
        model = 'local/qwen-coder'
        model_parameters = @{ temperature = 0.1 }
        tools = $Tools
        conversation_starters = $ConversationStarters
        category = 'RapidDraft Knowledge Bases'
        support_contact = @{ name = 'Adeel'; email = 'adeel@rapiddraft.ai' }
    }

    if ($null -ne $existing) {
        $updated = Invoke-LibreChatRequest -Client $Client -Method ([System.Net.Http.HttpMethod]::new('PATCH')) `
            -Path "/api/agents/$($existing.id)" -Content (New-JsonContent $payload)
        Write-Host "Reused and synchronized agent: $Name"
        return $updated
    }

    $created = Invoke-LibreChatRequest -Client $Client -Method ([System.Net.Http.HttpMethod]::Post) `
        -Path '/api/agents' -Content (New-JsonContent $payload) -ExpectedStatus @(201)
    Write-Host "Created agent: $Name"
    return $created
}

function Set-AgentGroupAccess {
    param(
        [System.Net.Http.HttpClient]$Client,
        $Agent,
        $Group
    )

    $resourceId = Get-PropertyValue -Object $Agent -Name '_id'
    if ([string]::IsNullOrWhiteSpace([string]$resourceId)) {
        $expanded = Invoke-LibreChatRequest -Client $Client -Method ([System.Net.Http.HttpMethod]::Get) `
            -Path "/api/agents/$($Agent.id)/expanded"
        $resourceId = Get-PropertyValue -Object $expanded -Name '_id'
    }
    if ([string]::IsNullOrWhiteSpace([string]$resourceId)) {
        throw "Could not resolve the MongoDB resource ID for agent $($Agent.name)."
    }

    $groupId = [string](Get-PropertyValue -Object $Group -Name '_id')
    $payload = @{
        updated = @(@{
            type = 'group'
            id = $groupId
            idOnTheSource = $groupId
            name = $Group.name
            source = 'local'
            accessRoleId = 'agent_viewer'
        })
        removed = @()
        public = $false
    }
    $null = Invoke-LibreChatRequest -Client $Client -Method ([System.Net.Http.HttpMethod]::Put) `
        -Path "/api/permissions/agent/$resourceId" -Content (New-JsonContent $payload)
    Write-Host "Granted view/query access to '$($Group.name)' for agent '$($Agent.name)' (not public)."
}

function Send-AgentFile {
    param(
        [System.Net.Http.HttpClient]$Client,
        [string]$AgentId,
        [System.IO.FileInfo]$File
    )

    $multipart = [System.Net.Http.MultipartFormDataContent]::new()
    try {
        $multipart.Add([System.Net.Http.StringContent]::new('RapidDraft Local AI'), 'endpoint')
        $multipart.Add([System.Net.Http.StringContent]::new('custom'), 'endpointType')
        $multipart.Add([System.Net.Http.StringContent]::new([Guid]::NewGuid().ToString()), 'file_id')
        $multipart.Add([System.Net.Http.StringContent]::new($AgentId), 'agent_id')
        $multipart.Add([System.Net.Http.StringContent]::new('file_search'), 'tool_resource')
        $multipart.Add([System.Net.Http.StringContent]::new('local/qwen-coder'), 'model')

        $bytes = [System.IO.File]::ReadAllBytes($File.FullName)
        $fileContent = [System.Net.Http.ByteArrayContent]::new($bytes)
        $fileContent.Headers.ContentType = [System.Net.Http.Headers.MediaTypeHeaderValue]::new('text/markdown')
        $multipart.Add($fileContent, 'file', $File.Name)

        return Invoke-LibreChatRequest -Client $Client -Method ([System.Net.Http.HttpMethod]::Post) `
            -Path '/api/files' -Content $multipart
    }
    finally {
        $multipart.Dispose()
    }
}

function Get-AgentFiles {
    param(
        [System.Net.Http.HttpClient]$Client,
        [string]$AgentId
    )

    $result = Invoke-LibreChatRequest -Client $Client -Method ([System.Net.Http.HttpMethod]::Get) `
        -Path "/api/files/agent/$AgentId"
    foreach ($item in $result) {
        Write-Output $item
    }
}

function Remove-AgentFiles {
    param(
        [System.Net.Http.HttpClient]$Client,
        [string]$AgentId,
        [object[]]$Files
    )

    if ($Files.Count -eq 0) {
        return
    }
    $payloadFiles = @($Files | ForEach-Object {
        @{
            file_id = $_.file_id
            filepath = $_.filepath
            source = $_.source
            filename = $_.filename
        }
    })
    $payload = @{
        files = $payloadFiles
        agent_id = $AgentId
        tool_resource = 'file_search'
    }
    $null = Invoke-LibreChatRequest -Client $Client -Method ([System.Net.Http.HttpMethod]::Delete) `
        -Path '/api/files' -Content (New-JsonContent $payload)
}

function Repair-CorpusCheckpoint {
    param(
        [System.Net.Http.HttpClient]$Client,
        [hashtable]$State,
        [string]$CorpusKey,
        [System.IO.FileInfo[]]$LocalFiles,
        $Agent
    )

    $corpusState = $State.uploaded[$CorpusKey]
    $localByName = @{}
    foreach ($file in $LocalFiles) {
        $localByName[$file.Name] = $file
    }

    $remoteFiles = @(Get-AgentFiles -Client $Client -AgentId $Agent.id)
    $unexpected = @($remoteFiles | Where-Object { -not $localByName.ContainsKey($_.filename) })
    if ($unexpected.Count -gt 0) {
        $unexpectedNames = @($unexpected | ForEach-Object { $_.filename }) -join ', '
        throw "Agent '$($Agent.name)' contains $($unexpected.Count) files outside the managed corpus: $unexpectedNames. Refusing to alter them."
    }

    $adopted = 0
    $duplicates = @()
    foreach ($group in @($remoteFiles | Group-Object filename)) {
        $name = $group.Name
        $embeddedCopies = @($group.Group | Where-Object { $_.embedded -eq $true })
        if ($embeddedCopies.Count -eq 0) {
            continue
        }

        $keep = $null
        if ($corpusState.ContainsKey($name)) {
            $trackedId = [string]$corpusState[$name].fileId
            $keep = @($embeddedCopies | Where-Object { $_.file_id -eq $trackedId }) | Select-Object -First 1
        }
        if ($null -eq $keep) {
            $keep = $embeddedCopies | Select-Object -First 1
            $sourceFile = $localByName[$name]
            $hash = (Get-FileHash -LiteralPath $sourceFile.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
            $corpusState[$name] = @{
                sha256 = $hash
                fileId = [string]$keep.file_id
                embedded = $true
                uploadedAt = [DateTime]::UtcNow.ToString('o')
                recoveredFromRemote = $true
            }
            $adopted++
        }

        $duplicates += @($group.Group | Where-Object { $_.file_id -ne $keep.file_id })
    }

    if ($adopted -gt 0) {
        Save-State -State $State
        Write-Host "[$CorpusKey] recovered $adopted committed upload(s) from the remote agent after an interrupted response."
    }
    if ($duplicates.Count -gt 0) {
        Remove-AgentFiles -Client $Client -AgentId $Agent.id -Files $duplicates
        Write-Host "[$CorpusKey] removed $($duplicates.Count) duplicate remote file association(s)."
    }
}

function Import-Corpus {
    param(
        [System.Net.Http.HttpClient]$Client,
        [hashtable]$State,
        [string]$CorpusKey,
        [string]$Path,
        $Agent
    )

    $resolvedPath = (Resolve-Path -LiteralPath $Path).Path
    $files = @(Get-ChildItem -LiteralPath $resolvedPath -File -Filter '*.md' | Sort-Object Name)
    if ($files.Count -eq 0) {
        throw "No Markdown documents found in corpus path: $resolvedPath"
    }

    if (-not $State.uploaded.ContainsKey($CorpusKey)) {
        $State.uploaded[$CorpusKey] = @{}
    }
    $corpusState = $State.uploaded[$CorpusKey]
    Repair-CorpusCheckpoint -Client $Client -State $State -CorpusKey $CorpusKey `
        -LocalFiles $files -Agent $Agent
    $alreadyUploaded = 0
    $newlyUploaded = 0

    foreach ($file in $files) {
        $hash = (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
        $prior = if ($corpusState.ContainsKey($file.Name)) { $corpusState[$file.Name] } else { $null }
        if ($null -ne $prior -and $prior.sha256 -eq $hash -and -not [string]::IsNullOrWhiteSpace([string]$prior.fileId)) {
            $alreadyUploaded++
            continue
        }

        $result = $null
        for ($attempt = 1; $attempt -le 5; $attempt++) {
            try {
                $result = Send-AgentFile -Client $Client -AgentId $Agent.id -File $file
                break
            }
            catch {
                $message = $_.Exception.Message
                if ($message -notmatch 'HTTP (429|500|502|503|504)') {
                    throw
                }

                try {
                    $committed = @(Get-AgentFiles -Client $Client -AgentId $Agent.id | Where-Object {
                        $_.filename -eq $file.Name -and $_.embedded -eq $true
                    }) | Select-Object -First 1
                    if ($null -ne $committed) {
                        $result = $committed
                        Write-Host "[$CorpusKey] recovered '$($file.Name)' after a transient gateway response."
                        break
                    }
                }
                catch {
                    # The status check can share the same transient outage; the bounded retry below handles it.
                }

                if ($attempt -eq 5) {
                    throw
                }
                $delaySeconds = [Math]::Min(60, 5 * [Math]::Pow(2, $attempt - 1))
                Write-Host "[$CorpusKey] transient upload error for '$($file.Name)'; retrying in $delaySeconds seconds (attempt $($attempt + 1)/5)."
                Start-Sleep -Seconds $delaySeconds
            }
        }
        if ($null -eq $result) {
            throw "Upload for '$($file.Name)' did not produce a result."
        }
        $fileId = [string](Get-PropertyValue -Object $result -Name 'file_id')
        if ([string]::IsNullOrWhiteSpace($fileId)) {
            throw "Upload for '$($file.Name)' did not return a file_id."
        }
        $embedded = Get-PropertyValue -Object $result -Name 'embedded'
        if ($embedded -ne $true) {
            throw "Upload for '$($file.Name)' returned embedded='$embedded' instead of true."
        }

        $corpusState[$file.Name] = @{
            sha256 = $hash
            fileId = $fileId
            embedded = $true
            uploadedAt = [DateTime]::UtcNow.ToString('o')
        }
        $newlyUploaded++
        Save-State -State $State

        $completed = $alreadyUploaded + $newlyUploaded
        if (($newlyUploaded -eq 1) -or ($completed % $ProgressInterval -eq 0) -or ($completed -eq $files.Count)) {
            Write-Host "[$CorpusKey] $completed/$($files.Count) documents embedded"
        }
    }

    Write-Host "[$CorpusKey] ingestion complete: $($files.Count) total ($newlyUploaded new, $alreadyUploaded resumed)."
    return $files.Count
}

function Test-AgentFiles {
    param(
        [System.Net.Http.HttpClient]$Client,
        $Agent,
        [int]$ExpectedCount
    )

    $files = @(Get-AgentFiles -Client $Client -AgentId $Agent.id)
    $notEmbedded = @($files | Where-Object { $_.embedded -ne $true })
    if ($files.Count -ne $ExpectedCount) {
        throw "Agent '$($Agent.name)' has $($files.Count) associated files; expected $ExpectedCount."
    }
    if ($notEmbedded.Count -gt 0) {
        throw "Agent '$($Agent.name)' has $($notEmbedded.Count) files that are not embedded."
    }
    Write-Host "Verified agent '$($Agent.name)': $($files.Count) files, all embedded."
    foreach ($file in $files) {
        Write-Output $file
    }
}

function Invoke-AgentQueryTest {
    param(
        [System.Net.Http.HttpClient]$Client,
        $Agent,
        [string]$Question,
        [string]$ExpectedPattern
    )

    $messageId = [Guid]::NewGuid().ToString()
    $payload = @{
        text = $Question
        endpoint = 'agents'
        agent_id = $Agent.id
        conversationId = $null
        messageId = $messageId
        parentMessageId = '00000000-0000-0000-0000-000000000000'
        responseMessageId = $null
        sender = 'User'
        isCreatedByUser = $true
        isTemporary = $false
        isRegenerate = $false
        clientTimestamp = [DateTime]::Now.ToString('yyyy-MM-ddTHH:mm:ss')
        timezone = 'Europe/Berlin'
    }
    $started = Invoke-LibreChatRequest -Client $Client -Method ([System.Net.Http.HttpMethod]::Post) `
        -Path '/api/agents/chat/agents' -Content (New-JsonContent $payload)
    if ([string]::IsNullOrWhiteSpace([string]$started.streamId)) {
        throw "Agent query for '$($Agent.name)' did not return a stream ID."
    }

    $conversationId = [string]$started.conversationId
    try {
        $assistantMessage = $null
        for ($poll = 1; $poll -le 120; $poll++) {
            try {
                $messages = Invoke-LibreChatRequest -Client $Client -Method ([System.Net.Http.HttpMethod]::Get) `
                    -Path "/api/messages/$([Uri]::EscapeDataString($conversationId))"
            }
            catch {
                if ($_.Exception.Message -notmatch 'HTTP (502|503|504)') {
                    throw
                }
                Start-Sleep -Seconds 2
                continue
            }

            foreach ($message in $messages) {
                if ((Get-PropertyValue -Object $message -Name 'isCreatedByUser') -eq $false) {
                    $assistantMessage = $message
                }
            }
            if ($null -ne $assistantMessage) {
                break
            }
            Start-Sleep -Seconds 2
        }

        if ($null -eq $assistantMessage) {
            throw "Agent '$($Agent.name)' did not persist a response within the query-test timeout."
        }

        $answer = $assistantMessage | ConvertTo-Json -Depth 30 -Compress
        $sawFileSearch = $answer -match 'file_search'
        if ($answer -notmatch $ExpectedPattern) {
            $preview = if ($answer.Length -gt 500) { $answer.Substring(0, 500) } else { $answer }
            throw "Agent '$($Agent.name)' answer did not match expected pattern '$ExpectedPattern'. Preview: $preview"
        }

        $preview = ($answer -replace '\s+', ' ').Trim()
        if ($preview.Length -gt 240) {
            $preview = $preview.Substring(0, 240) + '...'
        }
        if (-not $sawFileSearch) {
            throw "Agent '$($Agent.name)' completed without recording a file_search tool call. Response preview: $preview"
        }
        Write-Host "Query test passed for '$($Agent.name)' with file_search. Response preview: $preview"
        return @{
            agentId = $Agent.id
            fileSearchObserved = $true
            expectedPattern = $ExpectedPattern
            passedAt = [DateTime]::UtcNow.ToString('o')
        }
    }
    finally {
        try {
            $deletePayload = @{ arg = @{ conversationId = $conversationId } }
            $null = Invoke-LibreChatRequest -Client $Client -Method ([System.Net.Http.HttpMethod]::Delete) `
                -Path '/api/convos' -Content (New-JsonContent $deletePayload) -ExpectedStatus @(201)
        }
        catch {
            Write-Warning "Could not remove temporary query-test conversation $conversationId."
        }
    }
}

$BaseUrl = $BaseUrl.TrimEnd('/')
if (-not (Test-Path -LiteralPath $CredentialPath -PathType Leaf)) {
    throw "LibreChat admin credential not found: $CredentialPath"
}
if (-not $SkipUploads) {
    if (-not (Test-Path -LiteralPath $TestArchivePath -PathType Container)) {
        throw "Test Archive corpus not found: $TestArchivePath"
    }
    if (-not (Test-Path -LiteralPath $BauerPath -PathType Container)) {
        throw "Bauer corpus not found: $BauerPath"
    }
}

$state = @{
    schemaVersion = 1
    baseUrl = $BaseUrl
    agents = @{}
    groups = @{}
    uploaded = @{}
    createdAt = [DateTime]::UtcNow.ToString('o')
}
if (Test-Path -LiteralPath $StatePath -PathType Leaf) {
    $loaded = Get-Content -LiteralPath $StatePath -Raw | ConvertFrom-Json | ConvertTo-Hashtable
    if ($loaded.baseUrl -ne $BaseUrl) {
        throw "State file belongs to '$($loaded.baseUrl)', not '$BaseUrl'."
    }
    $state = $loaded
}

$handler = [System.Net.Http.HttpClientHandler]::new()
$handler.AutomaticDecompression = [System.Net.DecompressionMethods]::GZip -bor `
    [System.Net.DecompressionMethods]::Deflate
$client = [System.Net.Http.HttpClient]::new($handler)
$client.BaseAddress = [Uri]::new($BaseUrl)
$client.Timeout = [TimeSpan]::FromMinutes(15)
$client.DefaultRequestHeaders.TryAddWithoutValidation(
    'User-Agent',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36'
) | Out-Null

try {
    $health = Invoke-LibreChatRequest -Client $client -Method ([System.Net.Http.HttpMethod]::Get) -Path '/health'
    Write-Host "LibreChat health check passed at $BaseUrl."

    $credential = Import-Clixml -LiteralPath $CredentialPath
    $plainCredential = $credential.GetNetworkCredential()
    $loginPayload = @{ email = $plainCredential.UserName; password = $plainCredential.Password }
    $session = Invoke-LibreChatRequest -Client $client -Method ([System.Net.Http.HttpMethod]::Post) `
        -Path '/api/auth/login' -Content (New-JsonContent $loginPayload)
    $plainCredential = $null
    $loginPayload = $null

    if ($session.user.role -ne 'ADMIN') {
        throw "Authenticated user is '$($session.user.role)', not ADMIN."
    }
    $client.DefaultRequestHeaders.Authorization = `
        [System.Net.Http.Headers.AuthenticationHeaderValue]::new('Bearer', [string]$session.token)
    $session.token = $null
    Write-Host "Authenticated as ADMIN: $($session.user.email)"

    $config = Invoke-LibreChatRequest -Client $client -Method ([System.Net.Http.HttpMethod]::Get) -Path '/api/config'
    if ($config.interface.agents.use -ne $true -or $config.interface.agents.create -ne $true -or `
        $config.interface.agents.share -ne $true -or $config.interface.agents.public -ne $false) {
        throw 'Agent permissions are not configured for private, shareable knowledge-base provisioning.'
    }

    $endpointConfig = Invoke-LibreChatRequest -Client $client -Method ([System.Net.Http.HttpMethod]::Get) `
        -Path '/api/endpoints'
    $agentsEndpoint = Get-PropertyValue -Object $endpointConfig -Name 'agents'
    if ($null -ne $agentsEndpoint) {
        $agentCapabilities = @(Get-PropertyValue -Object $agentsEndpoint -Name 'capabilities')
        if ($agentCapabilities -notcontains 'file_search') {
            throw "LibreChat explicitly disabled the file_search capability for agents. Enabled capabilities: $($agentCapabilities -join ', ')"
        }
        Write-Host "Resolved agent endpoint capabilities: $($agentCapabilities -join ', ')"
    }
    else {
        throw 'LibreChat did not publish an agents endpoint after agent configuration was enabled.'
    }
    Write-Host 'Verified agent creation/sharing policy and file_search capability.'

    $testGroup = Get-OrCreateGroup -Client $client -Name 'KB - Test Archive' `
        -Description 'Members may query the isolated Test Archive knowledge-base agent.' `
        -AdminUserId $session.user.id
    $bauerGroup = Get-OrCreateGroup -Client $client -Name 'KB - Bauer Kompressoren' `
        -Description 'Members may query the isolated Bauer Kompressoren knowledge-base agent.' `
        -AdminUserId $session.user.id

    $testAgent = Get-OrCreateAgent -Client $client -Name 'Test Archive' `
        -Description 'Private retrieval agent for the existing RapidDraft Test Archive.' `
        -Instructions @'
You are the Test Archive knowledge-base assistant. For factual questions, always search the files attached to this agent before answering. Answer only from Test Archive files and never claim or infer information from another company's or project's knowledge base. Cite the source filename and page or section when the retrieved text provides it. If the answer is not present in these files, say clearly that it was not found in Test Archive. Do not silently fill gaps from general knowledge.
'@ `
        -ConversationStarters @('Summarize the Test Archive', 'Which source files discuss the requested topic?')

    $bauerAgent = Get-OrCreateAgent -Client $client -Name 'Bauer Kompressoren' `
        -Description 'Private retrieval agent for Bauer Kompressoren product and project material.' `
        -Instructions @'
You are the Bauer Kompressoren knowledge-base assistant. For factual questions, always search the files attached to this agent before answering. Answer only from Bauer Kompressoren files and never claim or infer information from Test Archive or another company's knowledge base. Cite the source filename and page or section when the retrieved text provides it. If the answer is not present in these files, say clearly that it was not found in Bauer Kompressoren. Do not silently fill gaps from general knowledge.

For similar-project, project-comparison, exact part, compatible-part, or structured document lookup, call search_bauer_twin_mcp_bauer-twin. Treat its medium, pressure, topology, and compressor-family exclusions as hard constraints. Every project, part, and compatibility record returned by that tool is synthetic demo data; say this explicitly and never present it as confirmed Bauer internal master data. Use file_search separately for quotations and page-level evidence from the uploaded public corpus. Do not invent an identifier or compatibility rule when neither tool returns it.
'@ `
        -ConversationStarters @('Find a similar previous project', 'Find a compatible part or document') `
        -Tools @('file_search', 'search_bauer_twin_mcp_bauer-twin')

    $testTools = @((Get-PropertyValue -Object $testAgent -Name 'tools'))
    $bauerTools = @((Get-PropertyValue -Object $bauerAgent -Name 'tools'))
    if ($testTools -notcontains 'file_search' -or `
        $bauerTools -notcontains 'file_search' -or `
        $bauerTools -notcontains 'search_bauer_twin_mcp_bauer-twin') {
        throw "Persisted agent tool configuration is invalid. Test Archive tools: $($testTools -join ', '); Bauer tools: $($bauerTools -join ', ')."
    }

    Set-AgentGroupAccess -Client $client -Agent $testAgent -Group $testGroup
    Set-AgentGroupAccess -Client $client -Agent $bauerAgent -Group $bauerGroup

    $state.agents['test-archive'] = @{ id = $testAgent.id; resourceId = $testAgent._id; name = $testAgent.name }
    $state.agents['bauer-kompressoren'] = @{ id = $bauerAgent.id; resourceId = $bauerAgent._id; name = $bauerAgent.name }
    $state.groups['test-archive'] = @{ id = $testGroup._id; name = $testGroup.name }
    $state.groups['bauer-kompressoren'] = @{ id = $bauerGroup._id; name = $bauerGroup.name }
    Save-State -State $state

    if ($SkipUploads) {
        $testRemote = Invoke-LibreChatRequest -Client $client -Method ([System.Net.Http.HttpMethod]::Get) `
            -Path "/api/files/agent/$($testAgent.id)"
        $bauerRemote = Invoke-LibreChatRequest -Client $client -Method ([System.Net.Http.HttpMethod]::Get) `
            -Path "/api/files/agent/$($bauerAgent.id)"
        if (-not $state.uploaded.ContainsKey('test-archive') -or `
            -not $state.uploaded.ContainsKey('bauer-kompressoren')) {
            throw 'SkipUploads requires an existing state file with checkpoints for both corpora.'
        }
        $recordedTestNames = @($state.uploaded['test-archive'].Keys)
        $recordedBauerNames = @($state.uploaded['bauer-kompressoren'].Keys)
        $untrackedTest = @($testRemote | Where-Object { $recordedTestNames -notcontains $_.filename })
        $untrackedBauer = @($bauerRemote | Where-Object { $recordedBauerNames -notcontains $_.filename })
        $duplicateTest = @($testRemote | Group-Object filename | Where-Object { $_.Count -gt 1 })
        $duplicateBauer = @($bauerRemote | Group-Object filename | Where-Object { $_.Count -gt 1 })
        Write-Host "Uploads skipped by request; remote association counts are Test Archive=$(@($testRemote).Count), Bauer=$(@($bauerRemote).Count)."
        if ($untrackedTest.Count -gt 0 -or $untrackedBauer.Count -gt 0) {
            throw "Untracked remote files found. Test Archive=$($untrackedTest.Count), Bauer=$($untrackedBauer.Count)."
        }
        if ($duplicateTest.Count -gt 0 -or $duplicateBauer.Count -gt 0) {
            throw "Duplicate remote filenames found. Test Archive=$($duplicateTest.Count), Bauer=$($duplicateBauer.Count)."
        }
        $testFiles = @(Test-AgentFiles -Client $client -Agent $testAgent -ExpectedCount $recordedTestNames.Count)
        $bauerFiles = @(Test-AgentFiles -Client $client -Agent $bauerAgent -ExpectedCount $recordedBauerNames.Count)
    }
    else {
        $testExpected = Import-Corpus -Client $client -State $state -CorpusKey 'test-archive' `
            -Path $TestArchivePath -Agent $testAgent
        $bauerExpected = Import-Corpus -Client $client -State $state -CorpusKey 'bauer-kompressoren' `
            -Path $BauerPath -Agent $bauerAgent

        $testFiles = @(Test-AgentFiles -Client $client -Agent $testAgent -ExpectedCount $testExpected)
        $bauerFiles = @(Test-AgentFiles -Client $client -Agent $bauerAgent -ExpectedCount $bauerExpected)
    }
    $testIds = @($testFiles | ForEach-Object { $_.file_id })
    $bauerIds = @($bauerFiles | ForEach-Object { $_.file_id })
    $intersection = @($testIds | Where-Object { $bauerIds -contains $_ })
    if ($intersection.Count -ne 0) {
        throw "Knowledge-base isolation failed: $($intersection.Count) file IDs are shared between agents."
    }

    $state.verification = @{
        testArchiveCount = $testFiles.Count
        bauerCount = $bauerFiles.Count
        sharedFileIds = 0
        allEmbedded = $true
        verifiedAt = [DateTime]::UtcNow.ToString('o')
    }
    if ($RunQueryTests) {
        $testQuery = Invoke-AgentQueryTest -Client $client -Agent $testAgent `
            -Question 'Search this knowledge base. What are the EPLAN public test documents? Cite the source filename.' `
            -ExpectedPattern '(?i)EPLAN'
        $bauerQuery = Invoke-AgentQueryTest -Client $client -Agent $bauerAgent `
            -Question 'Search this knowledge base. What is B-DETECTION used for? Cite the source filename.' `
            -ExpectedPattern '(?i)B-?DETECTION|sensor|detection'
        $state.queryTests = @{
            testArchive = $testQuery
            bauerKompressoren = $bauerQuery
        }
    }
    Save-State -State $state
    Write-Host 'Isolation verified: the two agents have disjoint file sets and every document is embedded.'
}
finally {
    if ($null -ne $client) {
        $client.Dispose()
    }
    if ($null -ne $handler) {
        $handler.Dispose()
    }
}
