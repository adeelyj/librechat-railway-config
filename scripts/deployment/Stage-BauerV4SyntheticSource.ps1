[CmdletBinding()]
param(
    [string]$SourcePath = (
        'D:\02_Code\LibreChat_Setup-rag-v4-answer-quality\services\' +
        'bauer-evidence-v4\resources\bauer-synthetic-demo-v1.html'
    ),
    [string]$OutputPath = (
        'D:\02_Code\LibreChat_Setup-rag-v4-answer-quality\tmp\' +
        'v4-deploy\synthetic-source-stage.json'
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
$Child = Join-Path $PSScriptRoot 'stage_v4_synthetic_source.py'
$ProjectId = '45bb0e8d-9eca-4973-8026-a3eddbd092b6'
$Environment = 'testing'
$WorkerServiceId = 'b2621053-a240-48c6-abed-6f48f31fe141'

if (-not (Test-Path -LiteralPath $SourcePath -PathType Leaf)) {
    throw 'Reviewed synthetic source file is absent.'
}
$parent = Split-Path -Parent $OutputPath
$null = New-Item -ItemType Directory -Force -Path $parent

$start = [Diagnostics.ProcessStartInfo]::new()
$start.FileName = $Railway
$start.WorkingDirectory = 'D:\02_Code\LibreChat_Setup-rag-v4-answer-quality'
$start.ArgumentList.Add('run')
$start.ArgumentList.Add('--no-local')
$start.ArgumentList.Add('--project')
$start.ArgumentList.Add($ProjectId)
$start.ArgumentList.Add('--environment')
$start.ArgumentList.Add($Environment)
$start.ArgumentList.Add('--service')
$start.ArgumentList.Add($WorkerServiceId)
$start.ArgumentList.Add('--')
$start.ArgumentList.Add($Python)
$start.ArgumentList.Add($Child)
$start.ArgumentList.Add('--source')
$start.ArgumentList.Add($SourcePath)
$start.ArgumentList.Add('--output')
$start.ArgumentList.Add($OutputPath)
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
    $null = $stderr.Result
    if ($process.ExitCode -ne 0) {
        throw 'Synthetic source staging failed; child output was suppressed.'
    }
    $result = $stdout.Result | ConvertFrom-Json
    if (
        -not $result.read_after_write_verified -or
        $result.credentials_or_connection_details_emitted
    ) {
        throw 'Synthetic source staging did not return a safe verified result.'
    }
    $manifest = Get-Content -Raw -LiteralPath $OutputPath | ConvertFrom-Json
    if (
        -not $manifest.read_after_write_verified -or
        $manifest.credentials_or_connection_details_emitted
    ) {
        throw 'Synthetic source manifest is not safe and verified.'
    }
    [ordered]@{
        output_path = $OutputPath
        content_sha256 = [string]$manifest.content_sha256
        read_after_write_verified = $true
        credentials_or_connection_details_emitted = $false
    } | ConvertTo-Json -Compress
}
finally {
    $process.Dispose()
}
