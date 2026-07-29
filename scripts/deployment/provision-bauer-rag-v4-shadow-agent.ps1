[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'High')]
param(
    [string]$BaseUrl = 'https://chat.rapiddraft.ai',
    [string]$CredentialPath = 'D:\02_Code\auth\auth\librechat\testing-admin.credential.xml',
    [string]$ProvisionStatePath = 'D:\02_Code\LibreChat_Setup\tmp\knowledge-base-provision-state.json',
    [string]$V1AgentId = 'agent_Z8A2LtQWLeP4KuUDbvqZL',
    [string]$V2AgentId = 'agent_pmPMcA25UXS7vznUaz-DU',
    [string]$V3AgentId = 'agent_DzeT_ugU3tuZC_VCKB8Bh',
    [string]$TestArchiveAgentId = 'agent_QnRNYPGlShuSnY0CYgQnm',
    [string]$ExpectedV1AgentName = 'Bauer Kompressoren',
    [string]$ExpectedV2AgentName = 'Bauer Kompressoren - RAG v2 Test',
    [string]$ExpectedV3AgentName = 'Bauer Kompressoren - RAG V3 Private Shadow',
    [string]$ExpectedTestArchiveAgentName = 'Test Archive',
    [int]$ExpectedBauerFileCount = 373,
    [int]$ExpectedTestArchiveFileCount = 41,
    [Parameter(Mandatory = $true)]
    [AllowEmptyString()]
    [string]$RagV2SelectorValue,
    [Parameter(Mandatory = $true)]
    [AllowEmptyString()]
    [string]$BauerV4SelectorValue,
    [string]$V4AgentName = '',
    [switch]$Apply,
    [string]$ReportPath = ''
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
Add-Type -AssemblyName System.Net.Http

function Get-PropertyValue {
    param(
        $Object,
        [Parameter(Mandatory = $true)][string]$Name
    )

    if ($null -eq $Object) {
        return $null
    }
    if ($Object -is [System.Collections.IDictionary]) {
        if ($Object.Contains($Name)) {
            return $Object[$Name]
        }
        return $null
    }
    $property = $Object.PSObject.Properties[$Name]
    if ($null -eq $property) {
        return $null
    }
    return $property.Value
}

function Get-CollectionCount {
    param($Value)

    if ($null -eq $Value) {
        return 0
    }
    if (
        ($Value -is [System.Collections.IEnumerable]) -and
        -not ($Value -is [string]) -and
        -not ($Value -is [System.Collections.IDictionary])
    ) {
        return @($Value).Count
    }
    return 1
}

function Get-ObjectPropertyNames {
    param($Object)

    if ($null -eq $Object) {
        return
    }
    if ($Object -is [System.Collections.IDictionary]) {
        foreach ($key in $Object.Keys) {
            Write-Output ([string]$key)
        }
        return
    }
    if ($Object -is [System.Management.Automation.PSCustomObject]) {
        foreach ($property in $Object.PSObject.Properties) {
            Write-Output ([string]$property.Name)
        }
    }
}

function Get-StringValues {
    param(
        $Value,
        [string]$Context = 'value'
    )

    if ($null -eq $Value) {
        return
    }
    $values = if (
        ($Value -is [System.Collections.IEnumerable]) -and
        -not ($Value -is [string]) -and
        -not ($Value -is [System.Collections.IDictionary])
    ) {
        @($Value)
    }
    else {
        @($Value)
    }
    foreach ($item in $values) {
        if ($null -eq $item) {
            throw "[$Context] Null string values are not allowed."
        }
        $text = [string]$item
        if ([string]::IsNullOrWhiteSpace($text)) {
            throw "[$Context] Empty string values are not allowed."
        }
        Write-Output $text
    }
}

function Get-SortedUniqueStrings {
    param(
        [object[]]$Values,
        [string]$Context = 'value'
    )

    $seen = [System.Collections.Generic.HashSet[string]]::new(
        [System.StringComparer]::Ordinal
    )
    $list = [System.Collections.Generic.List[string]]::new()
    foreach ($value in @($Values)) {
        if ($null -eq $value) {
            throw "[$Context] Null string values are not allowed."
        }
        $text = [string]$value
        if ([string]::IsNullOrWhiteSpace($text)) {
            throw "[$Context] Empty string values are not allowed."
        }
        if (-not $seen.Add($text)) {
            throw "[$Context] Duplicate string values are not allowed."
        }
        $list.Add($text)
    }
    $list.Sort([System.StringComparer]::Ordinal)
    foreach ($item in $list) {
        Write-Output $item
    }
}

function Get-StringSha256 {
    param([AllowEmptyString()][string]$Value)

    $algorithm = [System.Security.Cryptography.SHA256]::Create()
    try {
        $bytes = [System.Text.Encoding]::UTF8.GetBytes($Value)
        return (
            [System.BitConverter]::ToString($algorithm.ComputeHash($bytes)) -replace '-', ''
        ).ToLowerInvariant()
    }
    finally {
        $algorithm.Dispose()
    }
}

function Get-JsonSha256 {
    param($Value)

    $json = if ($null -eq $Value) {
        'null'
    }
    else {
        $Value | ConvertTo-Json -Depth 50 -Compress
    }
    return Get-StringSha256 -Value $json
}

function Test-DeepEquivalent {
    param(
        $Left,
        $Right
    )

    if ($null -eq $Left -or $null -eq $Right) {
        return ($null -eq $Left -and $null -eq $Right)
    }

    $leftIsObject = (
        $Left -is [System.Collections.IDictionary] -or
        $Left -is [System.Management.Automation.PSCustomObject]
    )
    $rightIsObject = (
        $Right -is [System.Collections.IDictionary] -or
        $Right -is [System.Management.Automation.PSCustomObject]
    )
    if ($leftIsObject -or $rightIsObject) {
        if (-not ($leftIsObject -and $rightIsObject)) {
            return $false
        }
        $leftNames = @(Get-ObjectPropertyNames -Object $Left | Sort-Object)
        $rightNames = @(Get-ObjectPropertyNames -Object $Right | Sort-Object)
        if ($leftNames.Count -ne $rightNames.Count) {
            return $false
        }
        for ($index = 0; $index -lt $leftNames.Count; $index++) {
            if ($leftNames[$index] -cne $rightNames[$index]) {
                return $false
            }
            if (-not (Test-DeepEquivalent `
                -Left (Get-PropertyValue -Object $Left -Name $leftNames[$index]) `
                -Right (Get-PropertyValue -Object $Right -Name $rightNames[$index]))) {
                return $false
            }
        }
        return $true
    }

    $leftIsEnumerable = (
        $Left -is [System.Collections.IEnumerable] -and
        -not ($Left -is [string])
    )
    $rightIsEnumerable = (
        $Right -is [System.Collections.IEnumerable] -and
        -not ($Right -is [string])
    )
    if ($leftIsEnumerable -or $rightIsEnumerable) {
        if (-not ($leftIsEnumerable -and $rightIsEnumerable)) {
            return $false
        }
        $leftItems = @($Left)
        $rightItems = @($Right)
        if ($leftItems.Count -ne $rightItems.Count) {
            return $false
        }
        for ($index = 0; $index -lt $leftItems.Count; $index++) {
            if (-not (Test-DeepEquivalent -Left $leftItems[$index] -Right $rightItems[$index])) {
                return $false
            }
        }
        return $true
    }

    if ($Left -is [string] -or $Right -is [string]) {
        return ([string]$Left -ceq [string]$Right)
    }
    return ($Left -eq $Right)
}

function Test-StringSetEqual {
    param(
        [object[]]$Left,
        [object[]]$Right
    )

    $leftSorted = @(Get-SortedUniqueStrings -Values @($Left) -Context 'left set')
    $rightSorted = @(Get-SortedUniqueStrings -Values @($Right) -Context 'right set')
    if ($leftSorted.Count -ne $rightSorted.Count) {
        return $false
    }
    for ($index = 0; $index -lt $leftSorted.Count; $index++) {
        if ($leftSorted[$index] -cne $rightSorted[$index]) {
            return $false
        }
    }
    return $true
}

function New-JsonContent {
    param($Value)

    return [System.Net.Http.StringContent]::new(
        ($Value | ConvertTo-Json -Depth 50 -Compress),
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
    $response = $null
    if ($null -ne $Content) {
        $request.Content = $Content
    }
    try {
        $response = $Client.SendAsync($request).GetAwaiter().GetResult()
        $body = $response.Content.ReadAsStringAsync().GetAwaiter().GetResult()
        $status = [int]$response.StatusCode
        if ($ExpectedStatus -notcontains $status) {
            throw "LibreChat $($Method.Method) $Path returned HTTP $status."
        }
        if ([string]::IsNullOrWhiteSpace($body)) {
            return $null
        }
        try {
            return $body | ConvertFrom-Json
        }
        catch {
            throw "LibreChat $($Method.Method) $Path returned invalid JSON."
        }
    }
    finally {
        if ($null -ne $response) {
            $response.Dispose()
        }
        $request.Dispose()
    }
}

function Invoke-LibreChatLogin {
    param(
        [Parameter(Mandatory = $true)][System.Net.Http.HttpClient]$Client,
        [Parameter(Mandatory = $true)]$Payload
    )

    # Deliberately one request and no retry loop. A violation-protection refusal must
    # be allowed to expire without adding another login attempt.
    $request = [System.Net.Http.HttpRequestMessage]::new(
        [System.Net.Http.HttpMethod]::Post,
        '/api/auth/login'
    )
    $response = $null
    $request.Content = New-JsonContent -Value $Payload
    try {
        $response = $Client.SendAsync($request).GetAwaiter().GetResult()
        $body = $response.Content.ReadAsStringAsync().GetAwaiter().GetResult()
        $status = [int]$response.StatusCode
        if (-not $response.IsSuccessStatusCode) {
            $looksLikeBan = (
                $status -in @(403, 429) -or
                $body -match '(?i)\bban(ned)?\b|violation|too many|temporar(?:y|ily)|rate.?limit'
            )
            if ($looksLikeBan) {
                throw (
                    'LibreChat login was refused by violation protection or a temporary ban ' +
                    "(HTTP $status). No retry was attempted."
                )
            }
            throw "LibreChat login returned HTTP $status. No retry was attempted."
        }
        if ([string]::IsNullOrWhiteSpace($body)) {
            throw 'LibreChat login returned an empty response. No retry was attempted.'
        }
        try {
            return $body | ConvertFrom-Json
        }
        catch {
            throw 'LibreChat login returned invalid JSON. No retry was attempted.'
        }
    }
    finally {
        if ($null -ne $response) {
            $response.Dispose()
        }
        $request.Dispose()
    }
}

function Get-AgentExpanded {
    param(
        [System.Net.Http.HttpClient]$Client,
        [string]$AgentId
    )

    return Invoke-LibreChatRequest `
        -Client $Client `
        -Method ([System.Net.Http.HttpMethod]::Get) `
        -Path "/api/agents/$AgentId/expanded"
}

function Get-AgentFileIds {
    param(
        $Agent,
        [string]$Context
    )

    $resources = Get-PropertyValue -Object $Agent -Name 'tool_resources'
    $fileSearch = Get-PropertyValue -Object $resources -Name 'file_search'
    $rawFileIds = Get-PropertyValue -Object $fileSearch -Name 'file_ids'
    return @(
        Get-StringValues -Value $rawFileIds -Context "$Context file IDs"
    )
}

function Get-AgentPermissions {
    param(
        [System.Net.Http.HttpClient]$Client,
        $Agent
    )

    $resourceId = [string](Get-PropertyValue -Object $Agent -Name '_id')
    if ([string]::IsNullOrWhiteSpace($resourceId)) {
        throw 'An expanded Agent did not include its resource ID.'
    }
    return Invoke-LibreChatRequest `
        -Client $Client `
        -Method ([System.Net.Http.HttpMethod]::Get) `
        -Path "/api/permissions/agent/$resourceId"
}

function Get-PermissionSafeSummary {
    param($Permissions)

    $principals = @((Get-PropertyValue -Object $Permissions -Name 'principals'))
    $groups = @($principals | Where-Object {
        [string](Get-PropertyValue -Object $_ -Name 'type') -ieq 'group'
    })
    $users = @($principals | Where-Object {
        [string](Get-PropertyValue -Object $_ -Name 'type') -ieq 'user'
    })
    $roles = @($principals | Where-Object {
        [string](Get-PropertyValue -Object $_ -Name 'type') -ieq 'role'
    })
    $groupBindings = @($groups | ForEach-Object {
        [ordered]@{
            id = [string](Get-PropertyValue -Object $_ -Name 'id')
            access_role_id = [string](Get-PropertyValue -Object $_ -Name 'accessRoleId')
        }
    } | Sort-Object id, access_role_id)
    $principalBindingLines = @($principals | ForEach-Object {
        @(
            [string](Get-PropertyValue -Object $_ -Name 'type'),
            [string](Get-PropertyValue -Object $_ -Name 'id'),
            [string](Get-PropertyValue -Object $_ -Name 'accessRoleId')
        ) -join "`t"
    } | Sort-Object)
    $summary = [ordered]@{
        public = [bool](Get-PropertyValue -Object $Permissions -Name 'public')
        group_count = $groups.Count
        group_bindings = $groupBindings
        user_principal_count = $users.Count
        role_principal_count = $roles.Count
        principal_bindings_sha256 = Get-StringSha256 -Value (
            $principalBindingLines -join "`n"
        )
    }
    $summary.fingerprint_sha256 = Get-JsonSha256 -Value $summary
    return $summary
}

function Assert-PrivateGroupAccess {
    param(
        $Permissions,
        [string]$ExpectedGroupId,
        [string]$Context
    )

    if ([bool](Get-PropertyValue -Object $Permissions -Name 'public')) {
        throw "$Context is public; expected private access."
    }
    $principals = @((Get-PropertyValue -Object $Permissions -Name 'principals'))
    $groups = @($principals | Where-Object {
        [string](Get-PropertyValue -Object $_ -Name 'type') -ieq 'group'
    })
    if ($groups.Count -ne 1) {
        throw "$Context has $($groups.Count) group principals; expected exactly one."
    }
    $groupId = [string](Get-PropertyValue -Object $groups[0] -Name 'id')
    $accessRoleId = [string](Get-PropertyValue -Object $groups[0] -Name 'accessRoleId')
    if ($groupId -cne $ExpectedGroupId -or $accessRoleId -cne 'agent_viewer') {
        throw "$Context does not have the expected private agent_viewer group binding."
    }
}

function Assert-TargetPermissionPreflight {
    param(
        $Permissions,
        [string]$ExpectedGroupId,
        [string]$AdminUserId
    )

    $principals = @((Get-PropertyValue -Object $Permissions -Name 'principals'))
    $groups = @($principals | Where-Object {
        [string](Get-PropertyValue -Object $_ -Name 'type') -ieq 'group'
    })
    $unexpectedGroups = @($groups | Where-Object {
        [string](Get-PropertyValue -Object $_ -Name 'id') -cne $ExpectedGroupId
    })
    if ($unexpectedGroups.Count -gt 0 -or $groups.Count -gt 1) {
        throw 'The exact-name V4 Agent has an unexpected group permission; refusing to alter it.'
    }

    $roles = @($principals | Where-Object {
        [string](Get-PropertyValue -Object $_ -Name 'type') -ieq 'role'
    })
    if ($roles.Count -gt 0) {
        throw 'The exact-name V4 Agent has an unexpected role permission; refusing to alter it.'
    }

    $users = @($principals | Where-Object {
        [string](Get-PropertyValue -Object $_ -Name 'type') -ieq 'user'
    })
    $unexpectedUsers = @($users | Where-Object {
        [string](Get-PropertyValue -Object $_ -Name 'id') -cne $AdminUserId -or
        [string](Get-PropertyValue -Object $_ -Name 'accessRoleId') -cne 'agent_owner'
    })
    if ($unexpectedUsers.Count -gt 0) {
        throw 'The exact-name V4 Agent has an unexpected user permission; refusing to alter it.'
    }
}

function Get-AgentSafeSummary {
    param(
        $Agent,
        $Permissions,
        [string]$Context
    )

    $fileIds = @(Get-AgentFileIds -Agent $Agent -Context $Context)
    $sortedFileIds = @(
        Get-SortedUniqueStrings -Values $fileIds -Context "$Context file IDs"
    )
    $tools = @(
        Get-StringValues `
            -Value (Get-PropertyValue -Object $Agent -Name 'tools') `
            -Context "$Context tools"
    )
    $sortedTools = @(
        Get-SortedUniqueStrings -Values $tools -Context "$Context tools"
    )
    $mcpServerNames = @(
        Get-StringValues `
            -Value (Get-PropertyValue -Object $Agent -Name 'mcpServerNames') `
            -Context "$Context MCP server names"
    )
    $sortedMcpServerNames = @(
        Get-SortedUniqueStrings `
            -Values $mcpServerNames `
            -Context "$Context MCP server names"
    )
    $subagents = Get-PropertyValue -Object $Agent -Name 'subagents'
    $summary = [ordered]@{
        id = [string](Get-PropertyValue -Object $Agent -Name 'id')
        resource_id = [string](Get-PropertyValue -Object $Agent -Name '_id')
        name = [string](Get-PropertyValue -Object $Agent -Name 'name')
        description_sha256 = Get-StringSha256 -Value (
            [string](Get-PropertyValue -Object $Agent -Name 'description')
        )
        provider = [string](Get-PropertyValue -Object $Agent -Name 'provider')
        model = [string](Get-PropertyValue -Object $Agent -Name 'model')
        model_parameters_sha256 = Get-JsonSha256 -Value (
            Get-PropertyValue -Object $Agent -Name 'model_parameters'
        )
        instructions_sha256 = Get-StringSha256 -Value (
            [string](Get-PropertyValue -Object $Agent -Name 'instructions')
        )
        category = [string](Get-PropertyValue -Object $Agent -Name 'category')
        support_contact_sha256 = Get-JsonSha256 -Value (
            Get-PropertyValue -Object $Agent -Name 'support_contact'
        )
        tools = $sortedTools
        tool_count = $sortedTools.Count
        tool_resources_sha256 = Get-JsonSha256 -Value (
            Get-PropertyValue -Object $Agent -Name 'tool_resources'
        )
        tool_options_sha256 = Get-JsonSha256 -Value (
            Get-PropertyValue -Object $Agent -Name 'tool_options'
        )
        file_count = $sortedFileIds.Count
        file_ids_sha256 = Get-StringSha256 -Value ($sortedFileIds -join "`n")
        conversation_starter_count = Get-CollectionCount -Value (
            Get-PropertyValue -Object $Agent -Name 'conversation_starters'
        )
        conversation_starters_sha256 = Get-JsonSha256 -Value (
            Get-PropertyValue -Object $Agent -Name 'conversation_starters'
        )
        legacy_connected_agent_count = Get-CollectionCount -Value (
            Get-PropertyValue -Object $Agent -Name 'agent_ids'
        )
        legacy_connected_agents_sha256 = Get-JsonSha256 -Value (
            Get-PropertyValue -Object $Agent -Name 'agent_ids'
        )
        edge_count = Get-CollectionCount -Value (
            Get-PropertyValue -Object $Agent -Name 'edges'
        )
        edges_sha256 = Get-JsonSha256 -Value (
            Get-PropertyValue -Object $Agent -Name 'edges'
        )
        subagents_enabled = [bool](Get-PropertyValue -Object $subagents -Name 'enabled')
        subagents_allow_self = [bool](
            Get-PropertyValue -Object $subagents -Name 'allowSelf'
        )
        subagent_agent_count = Get-CollectionCount -Value (
            Get-PropertyValue -Object $subagents -Name 'agent_ids'
        )
        subagents_sha256 = Get-JsonSha256 -Value $subagents
        skill_count = Get-CollectionCount -Value (
            Get-PropertyValue -Object $Agent -Name 'skills'
        )
        skills_sha256 = Get-JsonSha256 -Value (
            Get-PropertyValue -Object $Agent -Name 'skills'
        )
        skills_enabled = [bool](
            Get-PropertyValue -Object $Agent -Name 'skills_enabled'
        )
        action_count = Get-CollectionCount -Value (
            Get-PropertyValue -Object $Agent -Name 'actions'
        )
        actions_sha256 = Get-JsonSha256 -Value (
            Get-PropertyValue -Object $Agent -Name 'actions'
        )
        tool_kwargs_sha256 = Get-JsonSha256 -Value (
            Get-PropertyValue -Object $Agent -Name 'tool_kwargs'
        )
        mcp_server_names = $sortedMcpServerNames
        mcp_server_count = $sortedMcpServerNames.Count
        permission = Get-PermissionSafeSummary -Permissions $Permissions
    }
    $summary.fingerprint_sha256 = Get-JsonSha256 -Value $summary
    return $summary
}

function ConvertFrom-AgentSelector {
    param(
        [AllowEmptyString()][string]$Value,
        [string]$Context
    )

    $values = [System.Collections.Generic.List[string]]::new()
    foreach ($part in @($Value -split ',')) {
        $candidate = $part.Trim()
        if ([string]::IsNullOrWhiteSpace($candidate)) {
            continue
        }
        if ($candidate -cnotmatch '\Aagent_[A-Za-z0-9_-]+\z') {
            throw "$Context contains a malformed Agent ID."
        }
        $values.Add($candidate)
    }
    return @(
        Get-SortedUniqueStrings -Values $values.ToArray() -Context $Context
    )
}

function Get-V4ConfigurationViolations {
    param(
        $Agent,
        [string]$ExpectedName,
        $V1Agent,
        [object[]]$ExpectedFileIds
    )

    $violations = [System.Collections.Generic.List[string]]::new()
    if ([string](Get-PropertyValue -Object $Agent -Name 'name') -cne $ExpectedName) {
        $violations.Add('name')
    }
    if (
        [string](Get-PropertyValue -Object $Agent -Name 'description') -cne
        'Private Bauer RAG V4 fixed-candidate shadow Agent; not a production promotion.'
    ) {
        $violations.Add('description')
    }
    if (-not [string]::IsNullOrEmpty(
        [string](Get-PropertyValue -Object $Agent -Name 'instructions')
    )) {
        $violations.Add('instructions_not_empty')
    }
    foreach ($field in @('provider', 'model', 'category')) {
        if (
            [string](Get-PropertyValue -Object $Agent -Name $field) -cne
            [string](Get-PropertyValue -Object $V1Agent -Name $field)
        ) {
            $violations.Add($field)
        }
    }
    foreach ($field in @('model_parameters', 'support_contact')) {
        if (-not (Test-DeepEquivalent `
            -Left (Get-PropertyValue -Object $Agent -Name $field) `
            -Right (Get-PropertyValue -Object $V1Agent -Name $field))) {
            $violations.Add($field)
        }
    }

    $tools = @(
        Get-StringValues `
            -Value (Get-PropertyValue -Object $Agent -Name 'tools') `
            -Context 'V4 tools'
    )
    if ($tools.Count -ne 1 -or $tools[0] -cne 'file_search') {
        $violations.Add('tools_not_exact_file_search')
    }
    $actualFileIds = @(Get-AgentFileIds -Agent $Agent -Context 'V4 Agent')
    if (
        $actualFileIds.Count -ne $ExpectedFileIds.Count -or
        -not (Test-StringSetEqual -Left $actualFileIds -Right $ExpectedFileIds)
    ) {
        $violations.Add('file_ids')
    }

    $resourceNames = @(
        Get-ObjectPropertyNames -Object (
            Get-PropertyValue -Object $Agent -Name 'tool_resources'
        )
    )
    if ($resourceNames.Count -ne 1 -or $resourceNames[0] -cne 'file_search') {
        $violations.Add('tool_resources_not_file_search_only')
    }
    if ((Get-CollectionCount -Value (
        Get-PropertyValue -Object $Agent -Name 'conversation_starters'
    )) -ne 0) {
        $violations.Add('conversation_starters')
    }
    if ((Get-CollectionCount -Value (
        Get-PropertyValue -Object $Agent -Name 'agent_ids'
    )) -ne 0) {
        $violations.Add('legacy_connected_agents')
    }
    if ((Get-CollectionCount -Value (
        Get-PropertyValue -Object $Agent -Name 'edges'
    )) -ne 0) {
        $violations.Add('edges')
    }
    $subagents = Get-PropertyValue -Object $Agent -Name 'subagents'
    if (
        [bool](Get-PropertyValue -Object $subagents -Name 'enabled') -or
        [bool](Get-PropertyValue -Object $subagents -Name 'allowSelf') -or
        (Get-CollectionCount -Value (
            Get-PropertyValue -Object $subagents -Name 'agent_ids'
        )) -ne 0
    ) {
        $violations.Add('subagents')
    }
    if ((Get-CollectionCount -Value (
        Get-PropertyValue -Object $Agent -Name 'skills'
    )) -ne 0) {
        $violations.Add('skills')
    }
    if ([bool](Get-PropertyValue -Object $Agent -Name 'skills_enabled')) {
        $violations.Add('skills_enabled')
    }
    if (@(Get-ObjectPropertyNames -Object (
        Get-PropertyValue -Object $Agent -Name 'tool_options'
    )).Count -ne 0) {
        $violations.Add('tool_options')
    }
    foreach ($field in @('actions', 'tool_kwargs', 'mcpServerNames')) {
        if ((Get-CollectionCount -Value (
            Get-PropertyValue -Object $Agent -Name $field
        )) -ne 0) {
            $violations.Add($field)
        }
    }

    foreach ($violation in $violations) {
        Write-Output $violation
    }
}

function New-V4AgentPayload {
    param(
        [string]$Name,
        $V1Agent,
        [object[]]$FileIds
    )

    return [ordered]@{
        name = $Name
        description = 'Private Bauer RAG V4 fixed-candidate shadow Agent; not a production promotion.'
        instructions = ''
        provider = Get-PropertyValue -Object $V1Agent -Name 'provider'
        model = Get-PropertyValue -Object $V1Agent -Name 'model'
        model_parameters = Get-PropertyValue -Object $V1Agent -Name 'model_parameters'
        tools = @('file_search')
        skills = @()
        skills_enabled = $false
        agent_ids = @()
        edges = @()
        conversation_starters = @()
        tool_resources = [ordered]@{
            file_search = [ordered]@{
                file_ids = @($FileIds)
            }
        }
        tool_options = [ordered]@{}
        subagents = [ordered]@{
            enabled = $false
            allowSelf = $false
            agent_ids = @()
        }
        category = Get-PropertyValue -Object $V1Agent -Name 'category'
        support_contact = Get-PropertyValue -Object $V1Agent -Name 'support_contact'
    }
}

function Get-ExactNameAgent {
    param(
        [System.Net.Http.HttpClient]$Client,
        [string]$Name
    )

    $encodedName = [System.Uri]::EscapeDataString($Name)
    $list = Invoke-LibreChatRequest `
        -Client $Client `
        -Method ([System.Net.Http.HttpMethod]::Get) `
        -Path "/api/agents?search=$encodedName&limit=100&requiredPermission=2"
    $matches = @(
        @((Get-PropertyValue -Object $list -Name 'data')) |
            Where-Object {
                [string](Get-PropertyValue -Object $_ -Name 'name') -ceq $Name
            }
    )
    if ($matches.Count -gt 1) {
        throw 'More than one Agent has the requested exact V4 name; refusing to choose one.'
    }
    if ($matches.Count -eq 0) {
        return $null
    }
    return $matches[0]
}

function Get-ExactGroup {
    param(
        [System.Net.Http.HttpClient]$Client,
        [string]$Name,
        [string]$ExpectedId
    )

    $encodedName = [System.Uri]::EscapeDataString($Name)
    $result = Invoke-LibreChatRequest `
        -Client $Client `
        -Method ([System.Net.Http.HttpMethod]::Get) `
        -Path "/api/admin/groups?search=$encodedName&source=local&limit=100&offset=0"
    $matches = @(
        @((Get-PropertyValue -Object $result -Name 'groups')) |
            Where-Object {
                $candidateId = [string](Get-PropertyValue -Object $_ -Name 'id')
                if ([string]::IsNullOrWhiteSpace($candidateId)) {
                    $candidateId = [string](Get-PropertyValue -Object $_ -Name '_id')
                }
                (
                    [string](Get-PropertyValue -Object $_ -Name 'name') -ceq $Name -and
                    $candidateId -ceq $ExpectedId
                )
            }
    )
    if ($matches.Count -ne 1) {
        throw 'The existing Bauer access group could not be resolved uniquely.'
    }
    return $matches[0]
}

function Compare-ProtectedSummaries {
    param(
        $Before,
        $After,
        [string]$Context
    )

    if (
        [string](Get-PropertyValue -Object $Before -Name 'fingerprint_sha256') -cne
        [string](Get-PropertyValue -Object $After -Name 'fingerprint_sha256')
    ) {
        throw "$Context changed while applying the V4 Agent."
    }
}

function Assert-AgentIdentity {
    param(
        $Agent,
        [string]$ExpectedId,
        [string]$ExpectedName,
        [string]$Context
    )

    if ([string](Get-PropertyValue -Object $Agent -Name 'id') -cne $ExpectedId) {
        throw "$Context returned an unexpected Agent ID."
    }
    if ([string](Get-PropertyValue -Object $Agent -Name 'name') -cne $ExpectedName) {
        throw "$Context returned an unexpected Agent name."
    }
}

$handler = $null
$client = $null
$credential = $null
$networkCredential = $null
$loginPayload = $null
$session = $null
$sessionToken = $null
$report = $null

try {
    if (-not (Test-Path -LiteralPath $CredentialPath -PathType Leaf)) {
        throw 'The DPAPI-encrypted LibreChat administrator credential file was not found.'
    }
    if (-not (Test-Path -LiteralPath $ProvisionStatePath -PathType Leaf)) {
        throw 'The LibreChat provision-state file was not found.'
    }
    foreach (
        $agentId in @(
            $V1AgentId,
            $V2AgentId,
            $V3AgentId,
            $TestArchiveAgentId
        )
    ) {
        if ($agentId -cnotmatch '\Aagent_[A-Za-z0-9_-]+\z') {
            throw 'A protected Agent ID input is malformed.'
        }
    }
    $protectedAgentIds = @(
        @(
            $V1AgentId,
            $V2AgentId,
            $V3AgentId,
            $TestArchiveAgentId
        ) | Select-Object -Unique
    )
    if ($protectedAgentIds.Count -ne 4) {
        throw 'The protected V1, V2, V3, and Test Archive Agent IDs must be distinct.'
    }
    if ($ExpectedBauerFileCount -ne 373) {
        throw 'This fixed Bauer V4 shadow tool requires an exact expected file count of 373.'
    }

    $baseUri = [System.Uri]::new($BaseUrl.TrimEnd('/'))
    if (
        $baseUri.Scheme -cne 'https' -or
        -not [string]::IsNullOrWhiteSpace($baseUri.UserInfo) -or
        -not [string]::IsNullOrWhiteSpace($baseUri.Query) -or
        -not [string]::IsNullOrWhiteSpace($baseUri.Fragment) -or
        ($baseUri.AbsolutePath -ne '/')
    ) {
        throw 'BaseUrl must be an HTTPS origin without credentials, query, fragment, or path.'
    }

    $ragV2SelectorIds = @(
        ConvertFrom-AgentSelector -Value $RagV2SelectorValue -Context 'RAG_V2_AGENT_IDS input'
    )
    $bauerV4SelectorIds = @(
        ConvertFrom-AgentSelector -Value $BauerV4SelectorValue -Context 'BAUER_V4_AGENT_IDS input'
    )
    if (
        $ragV2SelectorIds.Count -ne 1 -or
        $ragV2SelectorIds[0] -cne $V2AgentId
    ) {
        throw 'The supplied RAG_V2_AGENT_IDS snapshot is not exactly the protected V2 Agent ID.'
    }
    foreach (
        $protectedId in @(
            $V1AgentId,
            $V2AgentId,
            $V3AgentId,
            $TestArchiveAgentId
        )
    ) {
        if ($bauerV4SelectorIds -ccontains $protectedId) {
            throw 'The supplied BAUER_V4_AGENT_IDS snapshot contains a protected V1/V2/V3 Agent ID.'
        }
    }
    if (@($ragV2SelectorIds | Where-Object { $bauerV4SelectorIds -ccontains $_ }).Count -gt 0) {
        throw 'The supplied V2 and V4 selector snapshots overlap.'
    }
    if ($Apply -and $bauerV4SelectorIds.Count -ne 0) {
        throw (
            'Apply requires the pre-route phase: BAUER_V4_AGENT_IDS must be supplied as empty. ' +
            'This tool never changes that selector.'
        )
    }

    $state = Get-Content -Raw -LiteralPath $ProvisionStatePath | ConvertFrom-Json
    try {
        $stateV1AgentId = [string]$state.agents.'bauer-kompressoren'.id
        $stateTestAgentId = [string]$state.agents.'test-archive'.id
        $bauerGroupId = [string]$state.groups.'bauer-kompressoren'.id
        $bauerGroupName = [string]$state.groups.'bauer-kompressoren'.name
        $testGroupId = [string]$state.groups.'test-archive'.id
    }
    finally {
        $state = $null
    }
    if (
        $stateV1AgentId -cne $V1AgentId -or
        $stateTestAgentId -cne $TestArchiveAgentId
    ) {
        throw 'The provision-state protected Agent IDs do not match the explicit inputs.'
    }
    if (
        [string]::IsNullOrWhiteSpace($bauerGroupId) -or
        [string]::IsNullOrWhiteSpace($bauerGroupName) -or
        [string]::IsNullOrWhiteSpace($testGroupId)
    ) {
        throw 'The provision state does not contain the existing Bauer and Test Archive groups.'
    }

    $handler = [System.Net.Http.HttpClientHandler]::new()
    $handler.AutomaticDecompression = (
        [System.Net.DecompressionMethods]::GZip -bor
        [System.Net.DecompressionMethods]::Deflate
    )
    $client = [System.Net.Http.HttpClient]::new($handler)
    $client.BaseAddress = $baseUri
    $client.Timeout = [System.TimeSpan]::FromMinutes(5)

    # The browser-shaped User-Agent is installed before the credential is decrypted
    # and before the single login request is built.
    $client.DefaultRequestHeaders.TryAddWithoutValidation(
        'User-Agent',
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 ' +
        '(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36'
    ) | Out-Null
    $client.DefaultRequestHeaders.TryAddWithoutValidation(
        'Accept',
        'application/json, text/plain, */*'
    ) | Out-Null
    $client.DefaultRequestHeaders.TryAddWithoutValidation(
        'Accept-Language',
        'en-US,en;q=0.9,de;q=0.8'
    ) | Out-Null

    $credential = Import-Clixml -LiteralPath $CredentialPath
    $networkCredential = $credential.GetNetworkCredential()
    $loginPayload = [ordered]@{
        email = $networkCredential.UserName
        password = $networkCredential.Password
    }
    try {
        # Exactly one login. The bearer is reused by the one HttpClient below.
        $session = Invoke-LibreChatLogin -Client $client -Payload $loginPayload
    }
    finally {
        $loginPayload = $null
        $networkCredential = $null
        $credential = $null
    }

    $sessionUser = Get-PropertyValue -Object $session -Name 'user'
    if ([string](Get-PropertyValue -Object $sessionUser -Name 'role') -cne 'ADMIN') {
        throw 'The authenticated LibreChat user is not the existing administrator.'
    }
    $adminUserId = [string](Get-PropertyValue -Object $sessionUser -Name 'id')
    $sessionToken = [string](Get-PropertyValue -Object $session -Name 'token')
    if (
        [string]::IsNullOrWhiteSpace($adminUserId) -or
        [string]::IsNullOrWhiteSpace($sessionToken)
    ) {
        throw 'LibreChat login did not return the required administrator session fields.'
    }
    $client.DefaultRequestHeaders.Authorization = (
        [System.Net.Http.Headers.AuthenticationHeaderValue]::new('Bearer', $sessionToken)
    )
    $session.token = $null
    $sessionToken = $null

    $v1 = Get-AgentExpanded -Client $client -AgentId $V1AgentId
    $v2 = Get-AgentExpanded -Client $client -AgentId $V2AgentId
    $v3 = Get-AgentExpanded -Client $client -AgentId $V3AgentId
    $testArchive = Get-AgentExpanded -Client $client -AgentId $TestArchiveAgentId
    Assert-AgentIdentity `
        -Agent $v1 `
        -ExpectedId $V1AgentId `
        -ExpectedName $ExpectedV1AgentName `
        -Context 'Bauer V1'
    Assert-AgentIdentity `
        -Agent $v2 `
        -ExpectedId $V2AgentId `
        -ExpectedName $ExpectedV2AgentName `
        -Context 'Bauer V2'
    Assert-AgentIdentity `
        -Agent $v3 `
        -ExpectedId $V3AgentId `
        -ExpectedName $ExpectedV3AgentName `
        -Context 'Bauer V3'
    Assert-AgentIdentity `
        -Agent $testArchive `
        -ExpectedId $TestArchiveAgentId `
        -ExpectedName $ExpectedTestArchiveAgentName `
        -Context 'Test Archive'

    $v1Permissions = Get-AgentPermissions -Client $client -Agent $v1
    $v2Permissions = Get-AgentPermissions -Client $client -Agent $v2
    $v3Permissions = Get-AgentPermissions -Client $client -Agent $v3
    $testPermissions = Get-AgentPermissions -Client $client -Agent $testArchive
    Assert-PrivateGroupAccess `
        -Permissions $v1Permissions `
        -ExpectedGroupId $bauerGroupId `
        -Context 'Bauer V1'
    Assert-PrivateGroupAccess `
        -Permissions $v2Permissions `
        -ExpectedGroupId $bauerGroupId `
        -Context 'Bauer V2'
    Assert-PrivateGroupAccess `
        -Permissions $v3Permissions `
        -ExpectedGroupId $bauerGroupId `
        -Context 'Bauer V3'
    Assert-PrivateGroupAccess `
        -Permissions $testPermissions `
        -ExpectedGroupId $testGroupId `
        -Context 'Test Archive'

    $v1FileIds = @(Get-AgentFileIds -Agent $v1 -Context 'Bauer V1')
    $v2FileIds = @(Get-AgentFileIds -Agent $v2 -Context 'Bauer V2')
    $v3FileIds = @(Get-AgentFileIds -Agent $v3 -Context 'Bauer V3')
    $testFileIds = @(Get-AgentFileIds -Agent $testArchive -Context 'Test Archive')
    $null = @(Get-SortedUniqueStrings -Values $v1FileIds -Context 'Bauer V1 file IDs')
    $null = @(Get-SortedUniqueStrings -Values $v2FileIds -Context 'Bauer V2 file IDs')
    $null = @(Get-SortedUniqueStrings -Values $v3FileIds -Context 'Bauer V3 file IDs')
    $null = @(Get-SortedUniqueStrings -Values $testFileIds -Context 'Test Archive file IDs')
    if ($v1FileIds.Count -ne $ExpectedBauerFileCount) {
        throw "Bauer V1 has $($v1FileIds.Count) files; expected exactly 373."
    }
    if ($v2FileIds.Count -ne $ExpectedBauerFileCount) {
        throw "Bauer V2 has $($v2FileIds.Count) files; expected exactly 373."
    }
    if ($v3FileIds.Count -ne $ExpectedBauerFileCount) {
        throw "Bauer V3 has $($v3FileIds.Count) files; expected exactly 373."
    }
    if ($testFileIds.Count -ne $ExpectedTestArchiveFileCount) {
        throw 'Test Archive does not have its expected protected file count.'
    }
    if (-not (Test-StringSetEqual -Left $v1FileIds -Right $v2FileIds)) {
        throw 'The live Bauer V1 and V2 file-ID sets are not identical.'
    }
    if (-not (Test-StringSetEqual -Left $v1FileIds -Right $v3FileIds)) {
        throw 'The live Bauer V1 and V3 file-ID sets are not identical.'
    }
    if (@($testFileIds | Where-Object { $v1FileIds -ccontains $_ }).Count -ne 0) {
        throw 'The live Bauer and Test Archive file-ID sets overlap.'
    }

    $v1Before = Get-AgentSafeSummary `
        -Agent $v1 `
        -Permissions $v1Permissions `
        -Context 'Bauer V1'
    $v2Before = Get-AgentSafeSummary `
        -Agent $v2 `
        -Permissions $v2Permissions `
        -Context 'Bauer V2'
    $v3Before = Get-AgentSafeSummary `
        -Agent $v3 `
        -Permissions $v3Permissions `
        -Context 'Bauer V3'
    $testBefore = Get-AgentSafeSummary `
        -Agent $testArchive `
        -Permissions $testPermissions `
        -Context 'Test Archive'

    $report = [ordered]@{
        schema_version = 1
        mode = if ($Apply) { 'apply' } else { 'baseline_only' }
        captured_at = [System.DateTime]::UtcNow.ToString('o')
        librechat_origin = $baseUri.GetLeftPart([System.UriPartial]::Authority)
        authentication = [ordered]@{
            browser_user_agent_installed_before_login = $true
            login_attempt_count = 1
            login_retried = $false
            administrator_role_verified = $true
            bearer_reused_in_one_http_client = $true
            bearer_or_password_emitted = $false
        }
        selectors = [ordered]@{
            source = 'explicit_inputs'
            rag_v2_count = $ragV2SelectorIds.Count
            rag_v2_ids_sha256 = Get-StringSha256 -Value ($ragV2SelectorIds -join "`n")
            bauer_v4_count = $bauerV4SelectorIds.Count
            bauer_v4_ids_sha256 = Get-StringSha256 -Value ($bauerV4SelectorIds -join "`n")
            invariants = [ordered]@{
                rag_v2_is_exactly_v2 = $true
                protected_v1_v2_v3_test_absent_from_v4 = $true
                v2_and_v4_disjoint = $true
                selector_mutation_attempted = $false
            }
        }
        protected_agents = [ordered]@{
            bauer_v1 = $v1Before
            bauer_v2 = $v2Before
            bauer_v3 = $v3Before
            test_archive = $testBefore
            bauer_v1_v2_v3_file_sets_identical = $true
            bauer_test_archive_file_sets_disjoint = $true
        }
        apply = [ordered]@{
            requested = [bool]$Apply
            should_process_approved = $false
            status = if ($Apply) { 'pending' } else { 'not_requested' }
            protected_agents_unchanged = $null
            direct_mongodb_write_attempted = $false
            selector_mutation_attempted = $false
            active_release_pointer_mutation_attempted = $false
        }
    }

    if ($Apply) {
        if ([string]::IsNullOrWhiteSpace($V4AgentName)) {
            throw 'Apply requires an explicit, unique V4AgentName.'
        }
        if (
            $V4AgentName -cnotmatch '(?i)\bV4\b' -or
            $V4AgentName -cnotmatch '(?i)\bshadow\b' -or
            $V4AgentName -match '[\x00-\x1f]'
        ) {
            throw 'V4AgentName must clearly contain V4 and shadow and contain no control characters.'
        }
        if ($V4AgentName -cin @(
            $ExpectedV1AgentName,
            $ExpectedV2AgentName,
            $ExpectedV3AgentName,
            $ExpectedTestArchiveAgentName
        )) {
            throw 'V4AgentName must not equal a protected Agent name.'
        }

        foreach ($requiredV1Field in @(
            'provider',
            'model',
            'model_parameters',
            'category',
            'support_contact'
        )) {
            if ($null -eq (Get-PropertyValue -Object $v1 -Name $requiredV1Field)) {
                throw "Bauer V1 does not expose required copy field '$requiredV1Field'."
            }
        }

        $bauerGroup = Get-ExactGroup `
            -Client $client `
            -Name $bauerGroupName `
            -ExpectedId $bauerGroupId
        $existingListItem = Get-ExactNameAgent -Client $client -Name $V4AgentName
        $existingExpanded = $null
        $existingPermissions = $null
        if ($null -ne $existingListItem) {
            $existingId = [string](Get-PropertyValue -Object $existingListItem -Name 'id')
            if (
                $existingId -cin @(
                    $V1AgentId,
                    $V2AgentId,
                    $V3AgentId,
                    $TestArchiveAgentId
                )
            ) {
                throw 'The exact-name V4 Agent resolves to a protected Agent ID.'
            }
            $existingExpanded = Get-AgentExpanded -Client $client -AgentId $existingId
            $existingPermissions = Get-AgentPermissions -Client $client -Agent $existingExpanded
            Assert-TargetPermissionPreflight `
                -Permissions $existingPermissions `
                -ExpectedGroupId $bauerGroupId `
                -AdminUserId $adminUserId
            foreach ($unclearableField in @(
                'actions',
                'tool_kwargs',
                'mcpServerNames'
            )) {
                if ((Get-CollectionCount -Value (
                    Get-PropertyValue -Object $existingExpanded -Name $unclearableField
                )) -ne 0) {
                    throw (
                        'The exact-name V4 Agent contains legacy executable configuration ' +
                        'that the current Agent update contract cannot clear; refusing to alter it.'
                    )
                }
            }
        }

        $payload = New-V4AgentPayload `
            -Name $V4AgentName `
            -V1Agent $v1 `
            -FileIds $v1FileIds
        $beforeViolations = @(
            if ($null -eq $existingExpanded) {
                'agent_missing'
            }
            else {
                Get-V4ConfigurationViolations `
                    -Agent $existingExpanded `
                    -ExpectedName $V4AgentName `
                    -V1Agent $v1 `
                    -ExpectedFileIds $v1FileIds
            }
        )

        $approved = $PSCmdlet.ShouldProcess(
            "$V4AgentName (private, exactly 373 existing Bauer file IDs)",
            'Create or synchronize the isolated file_search-only V4 shadow Agent and its Bauer group permission'
        )
        $report.apply.should_process_approved = [bool]$approved
        if (-not $approved) {
            $report.apply.status = 'not_applied_should_process_declined'
            $report.apply.plan = [ordered]@{
                exact_name_match_found = ($null -ne $existingExpanded)
                pre_apply_violation_count = $beforeViolations.Count
                file_count = $v1FileIds.Count
                file_ids_sha256 = $v1Before.file_ids_sha256
                tools = @('file_search')
                private = $true
                access_role_id = 'agent_viewer'
                group_id = $bauerGroupId
                instructions_empty = $true
                conversation_starters_empty = $true
                connected_agents_empty = $true
                subagents_disabled = $true
                approval_resume_configuration_absent = $true
            }
        }
        else {
            $mutationKinds = [System.Collections.Generic.List[string]]::new()
            if ($null -eq $existingExpanded) {
                $created = Invoke-LibreChatRequest `
                    -Client $client `
                    -Method ([System.Net.Http.HttpMethod]::Post) `
                    -Path '/api/agents' `
                    -Content (New-JsonContent -Value $payload) `
                    -ExpectedStatus @(201)
                $targetAgentId = [string](Get-PropertyValue -Object $created -Name 'id')
                if ([string]::IsNullOrWhiteSpace($targetAgentId)) {
                    throw 'LibreChat did not return an ID for the created V4 Agent.'
                }
                $mutationKinds.Add('agent_created')
            }
            else {
                $targetAgentId = [string](Get-PropertyValue -Object $existingExpanded -Name 'id')
                if ($beforeViolations.Count -gt 0) {
                    $null = Invoke-LibreChatRequest `
                        -Client $client `
                        -Method ([System.Net.Http.HttpMethod]::new('PATCH')) `
                        -Path "/api/agents/$targetAgentId" `
                        -Content (New-JsonContent -Value $payload)
                    $mutationKinds.Add('agent_synchronized')
                }
            }
            if (
                $targetAgentId -cin @(
                    $V1AgentId,
                    $V2AgentId,
                    $V3AgentId,
                    $TestArchiveAgentId
                )
            ) {
                throw 'LibreChat returned a protected Agent ID for the V4 operation.'
            }

            $targetExpanded = Get-AgentExpanded -Client $client -AgentId $targetAgentId
            $targetPermissionsBefore = Get-AgentPermissions -Client $client -Agent $targetExpanded
            Assert-TargetPermissionPreflight `
                -Permissions $targetPermissionsBefore `
                -ExpectedGroupId $bauerGroupId `
                -AdminUserId $adminUserId

            $targetPrincipals = @(
                Get-PropertyValue -Object $targetPermissionsBefore -Name 'principals'
            )
            $currentBauerBinding = @($targetPrincipals | Where-Object {
                [string](Get-PropertyValue -Object $_ -Name 'type') -ieq 'group' -and
                [string](Get-PropertyValue -Object $_ -Name 'id') -ceq $bauerGroupId -and
                [string](Get-PropertyValue -Object $_ -Name 'accessRoleId') -ceq 'agent_viewer'
            })
            $permissionNeedsUpdate = (
                [bool](Get-PropertyValue -Object $targetPermissionsBefore -Name 'public') -or
                $currentBauerBinding.Count -ne 1
            )
            if ($permissionNeedsUpdate) {
                $groupSource = [string](Get-PropertyValue -Object $bauerGroup -Name 'source')
                if ([string]::IsNullOrWhiteSpace($groupSource)) {
                    $groupSource = 'local'
                }
                $groupSourceId = [string](
                    Get-PropertyValue -Object $bauerGroup -Name 'idOnTheSource'
                )
                if ([string]::IsNullOrWhiteSpace($groupSourceId)) {
                    $groupSourceId = $bauerGroupId
                }
                $permissionPayload = [ordered]@{
                    updated = @([ordered]@{
                        type = 'group'
                        id = $bauerGroupId
                        idOnTheSource = $groupSourceId
                        name = [string](Get-PropertyValue -Object $bauerGroup -Name 'name')
                        source = $groupSource
                        accessRoleId = 'agent_viewer'
                    })
                    removed = @()
                    public = $false
                }
                $null = Invoke-LibreChatRequest `
                    -Client $client `
                    -Method ([System.Net.Http.HttpMethod]::Put) `
                    -Path "/api/permissions/agent/$(
                        [string](Get-PropertyValue -Object $targetExpanded -Name '_id')
                    )" `
                    -Content (New-JsonContent -Value $permissionPayload)
                $mutationKinds.Add('permission_synchronized')
                $permissionPayload = $null
            }

            $targetExpanded = Get-AgentExpanded -Client $client -AgentId $targetAgentId
            $targetPermissions = Get-AgentPermissions -Client $client -Agent $targetExpanded
            $afterViolations = @(
                Get-V4ConfigurationViolations `
                    -Agent $targetExpanded `
                    -ExpectedName $V4AgentName `
                    -V1Agent $v1 `
                    -ExpectedFileIds $v1FileIds
            )
            if ($afterViolations.Count -gt 0) {
                throw (
                    'V4 Agent readback failed one or more sealed configuration checks: ' +
                    ($afterViolations -join ', ') +
                    '.'
                )
            }
            Assert-PrivateGroupAccess `
                -Permissions $targetPermissions `
                -ExpectedGroupId $bauerGroupId `
                -Context 'Bauer V4 shadow'

            # Re-read every protected Agent after the additive V4 operation.
            $v1AfterAgent = Get-AgentExpanded -Client $client -AgentId $V1AgentId
            $v2AfterAgent = Get-AgentExpanded -Client $client -AgentId $V2AgentId
            $v3AfterAgent = Get-AgentExpanded -Client $client -AgentId $V3AgentId
            $testAfterAgent = Get-AgentExpanded -Client $client -AgentId $TestArchiveAgentId
            $v1After = Get-AgentSafeSummary `
                -Agent $v1AfterAgent `
                -Permissions (Get-AgentPermissions -Client $client -Agent $v1AfterAgent) `
                -Context 'Bauer V1 post-apply'
            $v2After = Get-AgentSafeSummary `
                -Agent $v2AfterAgent `
                -Permissions (Get-AgentPermissions -Client $client -Agent $v2AfterAgent) `
                -Context 'Bauer V2 post-apply'
            $v3After = Get-AgentSafeSummary `
                -Agent $v3AfterAgent `
                -Permissions (Get-AgentPermissions -Client $client -Agent $v3AfterAgent) `
                -Context 'Bauer V3 post-apply'
            $testAfter = Get-AgentSafeSummary `
                -Agent $testAfterAgent `
                -Permissions (Get-AgentPermissions -Client $client -Agent $testAfterAgent) `
                -Context 'Test Archive post-apply'
            Compare-ProtectedSummaries -Before $v1Before -After $v1After -Context 'Bauer V1'
            Compare-ProtectedSummaries -Before $v2Before -After $v2After -Context 'Bauer V2'
            Compare-ProtectedSummaries -Before $v3Before -After $v3After -Context 'Bauer V3'
            Compare-ProtectedSummaries `
                -Before $testBefore `
                -After $testAfter `
                -Context 'Test Archive'

            $targetSummary = Get-AgentSafeSummary `
                -Agent $targetExpanded `
                -Permissions $targetPermissions `
                -Context 'Bauer V4 shadow'
            $report.apply.status = if ($mutationKinds.Count -eq 0) {
                'reused_exact_idempotent_match'
            }
            elseif ($mutationKinds -contains 'agent_created') {
                'created_and_verified'
            }
            else {
                'synchronized_and_verified'
            }
            $report.apply.mutations = @($mutationKinds)
            $report.apply.target_agent = $targetSummary
            $report.apply.verification = [ordered]@{
                provider_model_parameters_category_contact_copied_from_v1 = $true
                file_search_only = $true
                exact_373_v1_file_ids = $true
                instructions_empty = $true
                conversation_starters_empty = $true
                connected_agents_empty = $true
                subagents_disabled = $true
                skills_empty = $true
                tool_options_empty = $true
                approval_resume_configuration_absent = $true
                private = $true
                bauer_group_agent_viewer = $true
                new_id_distinct_from_v1_v2_v3_test = $true
                protected_agents_unchanged = $true
                selector_still_requires_separate_later_deployment = $true
            }
            $report.apply.protected_agents_unchanged = $true
            $report.apply.next_configuration = [ordered]@{
                v4_api_allowlist_env = 'BAUER_V4_ALLOWED_AGENT_IDS_JSON'
                librechat_shadow_selector_env = 'BAUER_V4_AGENT_IDS'
                librechat_shadow_selector_changed_by_this_tool = $false
                production_promotion_performed = $false
            }
            $payload = $null
            $targetPrincipals = $null
        }
    }
}
catch {
    $safeMessage = [string]$_.Exception.Message
    $errorCode = if ($safeMessage -match 'temporary ban|violation protection') {
        'librechat_login_temporarily_refused'
    }
    elseif ($safeMessage -match 'No retry was attempted') {
        'librechat_login_failed_no_retry'
    }
    else {
        'v4_shadow_agent_tool_failed'
    }
    $report = [ordered]@{
        schema_version = 1
        status = 'error'
        error_code = $errorCode
        message = $safeMessage
        script_line_number = [int]$_.InvocationInfo.ScriptLineNumber
        script_stack = [string]$_.ScriptStackTrace
        login_retry_attempted = $false
        credentials_or_tokens_emitted = $false
        direct_mongodb_write_attempted = $false
        captured_at = [System.DateTime]::UtcNow.ToString('o')
    }
}
finally {
    if ($null -ne $client) {
        $client.DefaultRequestHeaders.Authorization = $null
    }
    $sessionToken = $null
    if ($null -ne $session) {
        $tokenProperty = $session.PSObject.Properties['token']
        if ($null -ne $tokenProperty) {
            $session.token = $null
        }
    }
    $session = $null
    $loginPayload = $null
    $networkCredential = $null
    $credential = $null
    if ($null -ne $client) {
        $client.Dispose()
    }
    if ($null -ne $handler) {
        $handler.Dispose()
    }
}

$jsonOutput = $report | ConvertTo-Json -Depth 50
if (-not [string]::IsNullOrWhiteSpace($ReportPath)) {
    $parent = Split-Path -Parent $ReportPath
    if (
        [string]::IsNullOrWhiteSpace($parent) -or
        -not (Test-Path -LiteralPath $parent -PathType Container)
    ) {
        throw 'ReportPath parent directory does not exist.'
    }
    if (Test-Path -LiteralPath $ReportPath) {
        throw 'Refusing to overwrite an existing LibreChat Agent report.'
    }
    [System.IO.File]::WriteAllText(
        $ReportPath,
        $jsonOutput,
        [System.Text.UTF8Encoding]::new($false)
    )
}
[System.Console]::Out.WriteLine($jsonOutput)
if (
    [string](Get-PropertyValue -Object $report -Name 'status') -ceq 'error'
) {
    exit 1
}
