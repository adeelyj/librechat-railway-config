$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

if ([string]::IsNullOrWhiteSpace($env:LOCALAI_CHAT_BASE_URL) -or `
    [string]::IsNullOrWhiteSpace($env:LOCALAI_CHAT_API_KEY)) {
    throw 'LOCALAI_CHAT_BASE_URL and LOCALAI_CHAT_API_KEY must be present in the environment.'
}

$uri = $env:LOCALAI_CHAT_BASE_URL.TrimEnd('/') + '/chat/completions'
$payload = @{
    model = 'local/qwen-coder'
    stream = $false
    messages = @(
        @{ role = 'system'; content = 'You are testing function calling. You must call lookup_knowledge exactly once before answering.' },
        @{ role = 'user'; content = 'Look up the EPLAN public test documents.' }
    )
    tools = @(@{
        type = 'function'
        function = @{
            name = 'lookup_knowledge'
            description = 'Retrieve indexed knowledge-base documents.'
            parameters = @{
                type = 'object'
                properties = @{
                    query = @{ type = 'string'; description = 'The search query.' }
                }
                required = @('query')
            }
        }
    })
    tool_choice = 'required'
}

$headers = @{ Authorization = "Bearer $($env:LOCALAI_CHAT_API_KEY)" }
$result = Invoke-RestMethod -Method Post -Uri $uri -Headers $headers -ContentType 'application/json' `
    -Body ($payload | ConvertTo-Json -Depth 15 -Compress) -TimeoutSec 180
$message = $result.choices[0].message
$toolCalls = @($message.tool_calls)
if ($toolCalls.Count -eq 0) {
    throw "The local model returned no tool calls. finish_reason=$($result.choices[0].finish_reason)"
}
if ($toolCalls[0].function.name -ne 'lookup_knowledge') {
    throw "The local model called '$($toolCalls[0].function.name)' instead of lookup_knowledge."
}

Write-Host "Local AI tool-calling test passed: model=$($result.model), finish_reason=$($result.choices[0].finish_reason), tool=$($toolCalls[0].function.name)."
