[CmdletBinding()]
param(
  [string]$RemoteHost = 'adeelyj@100.95.33.93',
  [string]$IdentityFile = 'D:\02_Code\auth\auth\local-server-adeel\local-server-adeel-codex_ed25519',
  [string]$OutputDirectory = 'D:\02_Code\LibreChat_Setup\tmp\corpora\test-archive'
)

$ErrorActionPreference = 'Stop'

if (Test-Path -LiteralPath $OutputDirectory) {
  $existing = @(Get-ChildItem -LiteralPath $OutputDirectory -Force)
  if ($existing.Count -gt 0) {
    throw "Output directory is not empty: $OutputDirectory"
  }
} else {
  New-Item -ItemType Directory -Path $OutputDirectory -Force | Out-Null
}

$sql = @'
SELECT json_build_object(
  'id', d.id,
  'title', d.title,
  'source_path', d.source_path,
  'mime_type', d.mime_type,
  'checksum', d.checksum,
  'status', d.status,
  'chunks', COALESCE(
    json_agg(
      json_build_object(
        'chunk_index', c.chunk_index,
        'page_number', c.page_number,
        'section_title', c.section_title,
        'source_kind', c.source_kind,
        'content', c.content
      ) ORDER BY c.chunk_index
    ) FILTER (WHERE c.id IS NOT NULL),
    '[]'::json
  )
)::text
FROM rag_documents d
LEFT JOIN rag_chunks c ON c.document_id = d.id
WHERE d.status = 'indexed'
GROUP BY d.id, d.title, d.source_path, d.mime_type, d.checksum, d.status
ORDER BY d.source_path;
'@

$sshArgs = @(
  '-o', 'BatchMode=yes',
  '-o', 'ConnectTimeout=15',
  '-i', $IdentityFile,
  $RemoteHost,
  'sudo', '-n', '-u', 'postgres',
  'psql', '-X', '-q', '-A', '-t', '-d', 'localai_rag'
)

$rows = @($sql | & ssh @sshArgs)
if ($LASTEXITCODE -ne 0) {
  throw "Remote PostgreSQL export failed with exit code $LASTEXITCODE"
}

$documents = @($rows | Where-Object { $_.Trim() } | ForEach-Object { $_ | ConvertFrom-Json })
if ($documents.Count -eq 0) {
  throw 'The Local AI archive export returned no indexed documents.'
}

$manifest = [System.Collections.Generic.List[object]]::new()
$ordinal = 0

foreach ($document in $documents) {
  $ordinal++
  $safeTitle = ($document.title -replace '[^\p{L}\p{Nd}._-]+', '-').Trim('-')
  if ([string]::IsNullOrWhiteSpace($safeTitle)) {
    $safeTitle = "document-$ordinal"
  }
  if ($safeTitle.Length -gt 100) {
    $safeTitle = $safeTitle.Substring(0, 100).TrimEnd('-')
  }
  $filename = '{0:D3}-{1}.md' -f $ordinal, $safeTitle
  $destination = Join-Path $OutputDirectory $filename

  $builder = [System.Text.StringBuilder]::new()
  [void]$builder.AppendLine("# $($document.title)")
  [void]$builder.AppendLine()
  [void]$builder.AppendLine(('- Original source: `{0}`' -f $document.source_path))
  [void]$builder.AppendLine(('- Original MIME type: `{0}`' -f $document.mime_type))
  [void]$builder.AppendLine(('- Original SHA-256: `{0}`' -f $document.checksum))
  [void]$builder.AppendLine(('- Local AI document ID: `{0}`' -f $document.id))
  [void]$builder.AppendLine()

  foreach ($chunk in @($document.chunks)) {
    $heading = "## Chunk $($chunk.chunk_index)"
    if ($null -ne $chunk.page_number) {
      $heading += " - page $($chunk.page_number)"
    }
    if (-not [string]::IsNullOrWhiteSpace($chunk.section_title)) {
      $heading += " - $($chunk.section_title)"
    }
    [void]$builder.AppendLine($heading)
    [void]$builder.AppendLine()
    [void]$builder.AppendLine([string]$chunk.content)
    [void]$builder.AppendLine()
  }

  [System.IO.File]::WriteAllText(
    $destination,
    $builder.ToString(),
    [System.Text.UTF8Encoding]::new($false)
  )

  $hash = (Get-FileHash -LiteralPath $destination -Algorithm SHA256).Hash.ToLowerInvariant()
  $manifest.Add([pscustomobject]@{
    filename = $filename
    sourcePath = $document.source_path
    sourceMimeType = $document.mime_type
    sourceChecksum = $document.checksum
    exportedChecksum = $hash
    chunkCount = @($document.chunks).Count
  })
}

$manifestPath = Join-Path $OutputDirectory 'manifest.json'
$manifestJson = $manifest | ConvertTo-Json -Depth 6
[System.IO.File]::WriteAllText(
  $manifestPath,
  $manifestJson,
  [System.Text.UTF8Encoding]::new($false)
)

[pscustomobject]@{
  outputDirectory = $OutputDirectory
  documentCount = $manifest.Count
  chunkCount = ($manifest | Measure-Object -Property chunkCount -Sum).Sum
  manifestPath = $manifestPath
}
