[CmdletBinding()]
param(
  [string]$BaseUrl = 'https://chat.rapiddraft.ai',
  [string]$Email = 'adeel@rapiddraft.ai',
  [string]$Name = 'Adeel',
  [string]$Username = 'adeel',
  [string]$CredentialPath = 'D:\02_Code\auth\auth\librechat\testing-admin.credential.xml'
)

$ErrorActionPreference = 'Stop'

function New-StrongPassword {
  $bytes = [byte[]]::new(30)
  $generator = [System.Security.Cryptography.RandomNumberGenerator]::Create()
  try {
    $generator.GetBytes($bytes)
  } finally {
    $generator.Dispose()
  }
  return ([Convert]::ToBase64String($bytes).TrimEnd('=') + '!aA9')
}

$health = Invoke-WebRequest -UseBasicParsing "$BaseUrl/health" -TimeoutSec 30
if ($health.StatusCode -ne 200 -or $health.Content.Trim() -ne 'OK') {
  throw "LibreChat health check failed at $BaseUrl/health"
}

if (Test-Path -LiteralPath $CredentialPath) {
  $credential = Import-Clixml -LiteralPath $CredentialPath
  if ($credential.UserName -ne $Email) {
    throw "The existing credential belongs to a different account: $($credential.UserName)"
  }
  $password = $credential.GetNetworkCredential().Password
  $createdCredential = $false
} else {
  $password = New-StrongPassword
  $createdCredential = $true

  $registration = @{
    name = $Name
    username = $Username
    email = $Email
    password = $password
    confirm_password = $password
  } | ConvertTo-Json

  Invoke-RestMethod `
    -Uri "$BaseUrl/api/auth/register" `
    -Method Post `
    -ContentType 'application/json' `
    -Body $registration `
    -TimeoutSec 60 | Out-Null
}

$login = @{
  email = $Email
  password = $password
} | ConvertTo-Json

$session = Invoke-RestMethod `
  -Uri "$BaseUrl/api/auth/login" `
  -Method Post `
  -ContentType 'application/json' `
  -Body $login `
  -SessionVariable webSession `
  -TimeoutSec 60

if (-not $session.token) {
  throw 'Admin login succeeded without returning an access token.'
}
if ($session.user.email -ne $Email -or $session.user.role -ne 'ADMIN') {
  throw "Unexpected bootstrap identity or role: $($session.user.email) / $($session.user.role)"
}

if ($createdCredential) {
  $credentialDirectory = Split-Path -Parent $CredentialPath
  New-Item -ItemType Directory -Path $credentialDirectory -Force | Out-Null
  $securePassword = ConvertTo-SecureString $password -AsPlainText -Force
  [pscredential]::new($Email, $securePassword) | Export-Clixml -LiteralPath $CredentialPath

  $acl = Get-Acl -LiteralPath $CredentialPath
  $acl.SetAccessRuleProtection($true, $false)
  $identity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
  $rule = [System.Security.AccessControl.FileSystemAccessRule]::new(
    $identity,
    [System.Security.AccessControl.FileSystemRights]::FullControl,
    [System.Security.AccessControl.AccessControlType]::Allow
  )
  $acl.SetAccessRule($rule)
  Set-Acl -LiteralPath $CredentialPath -AclObject $acl
}

[pscustomobject]@{
  email = $session.user.email
  role = $session.user.role
  credentialPath = $CredentialPath
  encryptedForCurrentWindowsUser = $true
  createdCredential = $createdCredential
}
