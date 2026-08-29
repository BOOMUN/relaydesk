param(
    [switch]$Force
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$envPath = Join-Path $projectRoot "infra\evolution\.env"

if ((Test-Path -LiteralPath $envPath) -and -not $Force) {
    Write-Output "Evolution environment already exists: $envPath"
    return
}

function New-RandomHex([int]$ByteCount) {
    $bytes = New-Object byte[] $ByteCount
    $generator = [Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $generator.GetBytes($bytes)
    }
    finally {
        $generator.Dispose()
    }
    return ($bytes | ForEach-Object { $_.ToString("x2") }) -join ""
}

$apiKey = New-RandomHex 32
$webhookSecret = New-RandomHex 32
$postgresPassword = New-RandomHex 24
$lines = @(
    "POSTGRES_DATABASE=evolution",
    "POSTGRES_USERNAME=evolution",
    "POSTGRES_PASSWORD=$postgresPassword",
    "EVOLUTION_API_KEY=$apiKey",
    "",
    "AGENTDESK_WHATSAPP_PROVIDER=evolution",
    "AGENTDESK_EVOLUTION_API_URL=http://127.0.0.1:8081",
    "AGENTDESK_EVOLUTION_API_KEY=$apiKey",
    "AGENTDESK_EVOLUTION_INSTANCE_NAME=agentdesk",
    "AGENTDESK_EVOLUTION_WEBHOOK_URL=http://host.docker.internal:8000/api/webhooks/evolution",
    "AGENTDESK_EVOLUTION_WEBHOOK_SECRET=$webhookSecret"
)
$encoding = [Text.UTF8Encoding]::new($false)
[IO.File]::WriteAllLines($envPath, $lines, $encoding)
Write-Output "Generated Evolution environment: $envPath"
