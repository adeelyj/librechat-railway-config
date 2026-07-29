[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$V3AgentId,
    [Parameter(Mandatory = $true)]
    [string]$V4AgentId,
    [Parameter(Mandatory = $true)]
    [string]$DeployedCommit,
    [string]$BaseUrl = 'https://chat.rapiddraft.ai',
    [string]$CredentialPath = (
        'D:\02_Code\auth\auth\librechat\testing-admin.credential.xml'
    ),
    [string]$PythonExe = (
        'D:\02_Code\LibreChat_Setup\tmp\v3-deploy\venv-api\Scripts\python.exe'
    ),
    [string]$V4Root = 'D:\02_Code\LibreChat_Setup-rag-v4',
    [string]$OutputPath = (
        'D:\02_Code\LibreChat_Setup-rag-v4\tmp\v4-deploy\evidence\' +
        'librechat-four-way-development-benchmark-20260729.json'
    )
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Net.Http

$Runner = Join-Path $PSScriptRoot 'four_way_librechat_benchmark.py'
$BackendExactCommit = '11ea00066300ed3ca5bf4ec75fd9d76a6d43dc59'
$AgentCaseTimeoutSeconds = '600'
$UnitTestFiles = @(
    'services/librechat-custom/tests/fileSearchBatch.test.js',
    'services/librechat-custom/tests/v3Authorization.test.js',
    'services/librechat-custom/tests/bauerV3FinalBoundary.test.js',
    'services/librechat-custom/tests/patchBauerV3FileSearchRequest.test.js',
    'services/librechat-custom/tests/patchBauerV3FinalBoundary.test.js',
    'services/librechat-custom/tests/patchBauerV3RunGraph.test.js'
    'services/librechat-custom/tests/v4Authorization.test.js'
)

function New-JsonContent {
    param([Parameter(Mandatory = $true)]$Value)
    $json = $Value | ConvertTo-Json -Depth 10 -Compress
    return [Net.Http.StringContent]::new(
        $json,
        [Text.Encoding]::UTF8,
        'application/json'
    )
}

function Join-NativeArguments {
    param([Parameter(Mandatory = $true)][string[]]$Values)
    return (
        $Values |
            ForEach-Object {
                '"' + ([string]$_).Replace('"', '\"') + '"'
            }
    ) -join ' '
}

function Invoke-CapturedNative {
    param(
        [Parameter(Mandatory = $true)][string]$FileName,
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [Parameter(Mandatory = $true)][string]$WorkingDirectory
    )

    $startInfo = [Diagnostics.ProcessStartInfo]::new()
    $startInfo.FileName = $FileName
    $startInfo.Arguments = Join-NativeArguments -Values $Arguments
    $startInfo.WorkingDirectory = $WorkingDirectory
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.WindowStyle = [Diagnostics.ProcessWindowStyle]::Hidden
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $native = [Diagnostics.Process]::new()
    $native.StartInfo = $startInfo
    try {
        if (-not $native.Start()) {
            throw 'A required offline verification process did not start.'
        }
        $stdoutTask = $native.StandardOutput.ReadToEndAsync()
        $stderrTask = $native.StandardError.ReadToEndAsync()
        $native.WaitForExit()
        return [pscustomobject]@{
            exit_code = $native.ExitCode
            stdout = $stdoutTask.GetAwaiter().GetResult()
            stderr = $stderrTask.GetAwaiter().GetResult()
        }
    }
    finally {
        $native.Dispose()
    }
}

function Get-StringSha256 {
    param([Parameter(Mandatory = $true)][AllowEmptyString()][string]$Value)

    $bytes = [Text.Encoding]::UTF8.GetBytes($Value)
    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        return ([BitConverter]::ToString(
            $sha.ComputeHash($bytes)
        )).Replace('-', '').ToLowerInvariant()
    }
    finally {
        $sha.Dispose()
        [Array]::Clear($bytes, 0, $bytes.Length)
    }
}

if (-not (Test-Path -LiteralPath $CredentialPath -PathType Leaf)) {
    throw 'DPAPI-encrypted LibreChat credential file is missing.'
}
if (-not (Test-Path -LiteralPath $Runner -PathType Leaf)) {
    throw "Regression runner is missing: $Runner"
}
if (-not (Test-Path -LiteralPath $PythonExe -PathType Leaf)) {
    throw "Regression Python runtime is missing: $PythonExe"
}
if (-not (Test-Path -LiteralPath $V4Root -PathType Container)) {
    throw "Pinned V4 worktree is missing: $V4Root"
}
if (
    $V3AgentId -cnotmatch '\Aagent_[A-Za-z0-9_-]+\z' -or
    $V3AgentId -in @(
    'agent_Z8A2LtQWLeP4KuUDbvqZL',
    'agent_pmPMcA25UXS7vznUaz-DU',
    'agent_QnRNYPGlShuSnY0CYgQnm'
) 
) {
    throw 'V3 Agent ID reuses a protected Agent ID.'
}
if (
    $V4AgentId -cnotmatch '\Aagent_[A-Za-z0-9_-]+\z' -or
    $V4AgentId -in @(
        'agent_Z8A2LtQWLeP4KuUDbvqZL',
        'agent_pmPMcA25UXS7vznUaz-DU',
        'agent_QnRNYPGlShuSnY0CYgQnm',
        $V3AgentId
    )
) {
    throw 'V4 Agent ID reuses a protected Agent ID.'
}
if ($DeployedCommit -cnotmatch '\A[0-9a-f]{40}\z') {
    throw 'DeployedCommit must be an exact Git commit.'
}
$baseUri = [Uri]::new($BaseUrl.TrimEnd('/'))
if (
    $baseUri.Scheme -cne 'https' -or
    -not [string]::IsNullOrWhiteSpace($baseUri.UserInfo) -or
    -not [string]::IsNullOrWhiteSpace($baseUri.Query) -or
    -not [string]::IsNullOrWhiteSpace($baseUri.Fragment) -or
    $baseUri.AbsolutePath -cne '/'
) {
    throw 'BaseUrl must be an HTTPS origin without credentials, query, fragment, or path.'
}
$baseOrigin = $baseUri.GetLeftPart([UriPartial]::Authority)

$handler = $null
$client = $null
$credential = $null
$networkCredential = $null
$payload = $null
$session = $null
$token = $null
$process = $null
$start = $null
$body = $null
$unitEvidenceSha256 = $null
$refreshUri = $null
$refreshTokenCookie = $null
try {
    $nodeCommand = Get-Command 'node.exe' -ErrorAction Stop
    $gitCommand = Get-Command 'git.exe' -ErrorAction Stop
    foreach ($relativePath in $UnitTestFiles) {
        if (-not (Test-Path -LiteralPath (
            Join-Path $V4Root $relativePath
        ) -PathType Leaf)) {
            throw "Pinned LibreChat unit test is missing: $relativePath"
        }
    }
    $commitResult = Invoke-CapturedNative `
        -FileName $gitCommand.Source `
        -Arguments @('-C', $V4Root, 'rev-parse', 'HEAD') `
        -WorkingDirectory $V4Root
    if (
        $commitResult.exit_code -ne 0 -or
        $commitResult.stdout.Trim() -cne $DeployedCommit
    ) {
        throw 'Regression worktree is not at the exact deployed LibreChat overlay commit.'
    }
    $commitResult = $null
    $backendAncestor = Invoke-CapturedNative `
        -FileName $gitCommand.Source `
        -Arguments @(
            '-C',
            $V4Root,
            'merge-base',
            '--is-ancestor',
            $BackendExactCommit,
            $DeployedCommit
        ) `
        -WorkingDirectory $V4Root
    if ($backendAncestor.exit_code -ne 0) {
        throw 'The deployed LibreChat overlay does not descend from the exact V3 backend commit.'
    }
    $backendAncestor = $null
    $backendDiff = Invoke-CapturedNative `
        -FileName $gitCommand.Source `
        -Arguments @(
            '-C',
            $V4Root,
            'diff',
            '--quiet',
            $BackendExactCommit,
            $DeployedCommit,
            '--',
            'services/bauer-evidence-v3'
        ) `
        -WorkingDirectory $V4Root
    if ($backendDiff.exit_code -ne 0) {
        throw 'V3 backend service files differ between the backend and overlay commits.'
    }
    $backendDiff = $null
    foreach ($relativePath in $UnitTestFiles) {
        $headBlob = Invoke-CapturedNative `
            -FileName $gitCommand.Source `
            -Arguments @(
                '-C', $V4Root, 'hash-object', '--', $relativePath
            ) `
            -WorkingDirectory $V4Root
        $overlayBlob = Invoke-CapturedNative `
            -FileName $gitCommand.Source `
            -Arguments @(
                '-C',
                $V4Root,
                'rev-parse',
                "${DeployedCommit}:$relativePath"
            ) `
            -WorkingDirectory $V4Root
        if (
            $headBlob.exit_code -ne 0 -or
            $overlayBlob.exit_code -ne 0 -or
            $headBlob.stdout.Trim() -cne $overlayBlob.stdout.Trim()
        ) {
            throw (
                'LibreChat unit-test source differs from the deployed ' +
                'overlay commit.'
            )
        }
        $headBlob = $null
        $overlayBlob = $null
    }
    $statusResult = Invoke-CapturedNative `
        -FileName $gitCommand.Source `
        -Arguments (
            @('-C', $V4Root, 'status', '--porcelain=v1', '--') +
            $UnitTestFiles
        ) `
        -WorkingDirectory $V4Root
    if (
        $statusResult.exit_code -ne 0 -or
        -not [string]::IsNullOrWhiteSpace($statusResult.stdout)
    ) {
        throw 'Pinned LibreChat unit-test sources have local changes.'
    }
    $statusResult = $null
    $nodeVersionResult = Invoke-CapturedNative `
        -FileName $nodeCommand.Source `
        -Arguments @('--version') `
        -WorkingDirectory $V4Root
    if (
        $nodeVersionResult.exit_code -ne 0 -or
        [string]::IsNullOrWhiteSpace($nodeVersionResult.stdout)
    ) {
        throw 'Node version verification failed before LibreChat unit tests.'
    }
    $nodeVersion = $nodeVersionResult.stdout.Trim()
    $nodeVersionResult = $null
    $unitResult = Invoke-CapturedNative `
        -FileName $nodeCommand.Source `
        -Arguments (
            @('--test') +
            @(
                $UnitTestFiles |
                    ForEach-Object { Join-Path $V4Root $_ }
            )
        ) `
        -WorkingDirectory $V4Root
    $unitOutput = [string]$unitResult.stdout
    if (
        $unitResult.exit_code -ne 0 -or
        $unitOutput -notmatch '(?m)^# tests 41\s*$' -or
        $unitOutput -notmatch '(?m)^# pass 41\s*$' -or
        $unitOutput -notmatch '(?m)^# fail 0\s*$'
    ) {
        $unitOutput = $null
        $unitResult = $null
        throw (
            'The exact 41 LibreChat fail-closed unit tests did not all pass; ' +
            'no login was attempted.'
        )
    }
    $unitHashLines = @(
        "backend_commit`t$BackendExactCommit"
        "overlay_commit`t$DeployedCommit"
        "node`t$nodeVersion"
        foreach ($relativePath in @($UnitTestFiles | Sort-Object)) {
            $sha = (
                Get-FileHash `
                    -LiteralPath (Join-Path $V4Root $relativePath) `
                    -Algorithm SHA256
            ).Hash.ToLowerInvariant()
            "$relativePath`t$sha"
        }
        'tests`t41'
        'pass`t41'
        'fail`t0'
    )
    $unitEvidenceSha256 = Get-StringSha256 -Value ($unitHashLines -join "`n")
    $unitHashLines = $null
    $unitOutput = $null
    $unitResult = $null
    $nodeVersion = $null

    $handler = [Net.Http.HttpClientHandler]::new()
    $handler.AutomaticDecompression = (
        [Net.DecompressionMethods]::GZip -bor
        [Net.DecompressionMethods]::Deflate
    )
    $client = [Net.Http.HttpClient]::new($handler)
    $client.BaseAddress = $baseUri
    $client.Timeout = [TimeSpan]::FromMinutes(3)
    [void]$client.DefaultRequestHeaders.TryAddWithoutValidation(
        'User-Agent',
        (
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) ' +
            'AppleWebKit/537.36 (KHTML, like Gecko) ' +
            'Chrome/138.0.0.0 Safari/537.36'
        )
    )
    [void]$client.DefaultRequestHeaders.TryAddWithoutValidation(
        'Accept',
        'application/json, text/plain, */*'
    )
    [void]$client.DefaultRequestHeaders.TryAddWithoutValidation(
        'Accept-Language',
        'en-US,en;q=0.9,de;q=0.8'
    )

    $credential = Import-Clixml -LiteralPath $CredentialPath
    $networkCredential = $credential.GetNetworkCredential()
    $payload = [ordered]@{
        email = $networkCredential.UserName
        password = $networkCredential.Password
    }
    $request = [Net.Http.HttpRequestMessage]::new(
        [Net.Http.HttpMethod]::Post,
        '/api/auth/login'
    )
    $request.Content = New-JsonContent -Value $payload
    $response = $null
    try {
        # Exactly one browser-shaped login; there is deliberately no retry path.
        $response = $client.SendAsync($request).GetAwaiter().GetResult()
        $body = $response.Content.ReadAsStringAsync().GetAwaiter().GetResult()
        if (-not $response.IsSuccessStatusCode) {
            throw (
                'LibreChat login failed or was temporarily refused with HTTP ' +
                "$([int]$response.StatusCode). No retry was attempted."
            )
        }
        $session = $body | ConvertFrom-Json
        $body = $null
    }
    finally {
        if ($null -ne $response) {
            $response.Dispose()
        }
        $request.Dispose()
        $payload = $null
        $networkCredential = $null
        $credential = $null
    }

    if ([string]$session.user.role -cne 'ADMIN') {
        throw 'LibreChat regression login did not return the existing administrator.'
    }
    $token = [string]$session.token
    if ([string]::IsNullOrWhiteSpace($token)) {
        throw 'LibreChat regression login returned no bearer token.'
    }
    $session.token = $null
    $refreshUri = [Uri]::new($baseOrigin + '/api/auth/refresh')
    $refreshCookies = @(
        $handler.CookieContainer.GetCookies($refreshUri) |
            Where-Object { $_.Name -ceq 'refreshToken' }
    )
    if (
        $refreshCookies.Count -ne 1 -or
        [string]::IsNullOrWhiteSpace([string]$refreshCookies[0].Value)
    ) {
        throw (
            'LibreChat login did not establish the in-memory refresh session ' +
            'required for a long serial regression.'
        )
    }
    $refreshTokenCookie = [string]$refreshCookies[0].Value
    $refreshCookies = $null

    $start = [Diagnostics.ProcessStartInfo]::new()
    $start.FileName = $PythonExe
    $runnerArguments = @(
        $Runner,
        '--base-url', $baseOrigin,
        '--v3-agent-id', $V3AgentId,
        '--v4-agent-id', $V4AgentId,
        '--output', $OutputPath,
        '--librechat-overlay-commit', $DeployedCommit,
        '--v3-candidate-backend-commit', $BackendExactCommit,
        '--v4-candidate-backend-commit', $DeployedCommit,
        '--unit-test-count', '41',
        '--unit-test-evidence-sha256', $unitEvidenceSha256,
        '--timeout', $AgentCaseTimeoutSeconds
    )
    $start.Arguments = Join-NativeArguments -Values $runnerArguments
    $start.UseShellExecute = $false
    $start.CreateNoWindow = $true
    $start.WindowStyle = [Diagnostics.ProcessWindowStyle]::Hidden
    $start.RedirectStandardOutput = $true
    $start.RedirectStandardError = $true
    $start.EnvironmentVariables['LIBRECHAT_TOKEN'] = $token
    $start.EnvironmentVariables['LIBRECHAT_REFRESH_TOKEN_COOKIE'] = (
        $refreshTokenCookie
    )
    $process = [Diagnostics.Process]::new()
    $process.StartInfo = $start
    if (-not $process.Start()) {
        throw 'LibreChat regression process did not start.'
    }
    [void]$start.EnvironmentVariables.Remove('LIBRECHAT_TOKEN')
    [void]$start.EnvironmentVariables.Remove(
        'LIBRECHAT_REFRESH_TOKEN_COOKIE'
    )
    $token = $null
    $refreshTokenCookie = $null
    $stdoutTask = $process.StandardOutput.ReadToEndAsync()
    $stderrTask = $process.StandardError.ReadToEndAsync()
    $process.WaitForExit()
    $stdout = $stdoutTask.GetAwaiter().GetResult()
    $stderr = $stderrTask.GetAwaiter().GetResult()
    if ($process.ExitCode -ne 0) {
        $stdout = $null
        $stderr = $null
        throw 'LibreChat shadow regression failed; child output was suppressed.'
    }
    $stderr = $null
    $safeResult = $stdout.Trim() | ConvertFrom-Json
    $stdout = $null
    $safeResult | ConvertTo-Json -Compress
}
finally {
    if ($null -ne $start) {
        [void]$start.EnvironmentVariables.Remove('LIBRECHAT_TOKEN')
        [void]$start.EnvironmentVariables.Remove(
            'LIBRECHAT_REFRESH_TOKEN_COOKIE'
        )
    }
    if ($null -ne $process) {
        $process.Dispose()
    }
    if ($null -ne $client) {
        $client.DefaultRequestHeaders.Authorization = $null
        $client.Dispose()
    }
    if ($null -ne $handler) {
        $handler.Dispose()
    }
    if ($null -ne $session) {
        $session.token = $null
    }
    $token = $null
    $refreshTokenCookie = $null
    $refreshUri = $null
    $payload = $null
    $networkCredential = $null
    $credential = $null
    $session = $null
    $body = $null
    $unitEvidenceSha256 = $null
    $baseOrigin = $null
    $baseUri = $null
    [GC]::Collect()
}
