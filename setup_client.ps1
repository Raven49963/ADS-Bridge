param(
    [string]$Python = 'python',
    [string]$ClientDirectory = (Join-Path $PSScriptRoot '.venv'),
    [string]$Runtime,
    [string]$AdsRoot = 'C:\Program Files\Keysight\ADS2026_Update0.1',
    [string]$WriteConfigPath
)

$ErrorActionPreference = 'Stop'
$venv = [System.IO.Path]::GetFullPath($ClientDirectory)
$client = Join-Path $venv 'Scripts\python.exe'
if (-not (Test-Path -LiteralPath $client)) {
    & $Python -m venv $venv
    if ($LASTEXITCODE -ne 0) { throw 'Could not create the MCP virtual environment.' }
}
& $client -m pip install -r (Join-Path $PSScriptRoot 'requirements-mcp.txt') | Out-Host
if ($LASTEXITCODE -ne 0) { throw 'Could not install MCP dependencies.' }

$envMap = @{}
if (Test-Path -LiteralPath $AdsRoot) {
    $envMap['HPEESOF_DIR'] = ([System.IO.Path]::GetFullPath($AdsRoot)).Replace('\', '/')
}
if ($Runtime) {
    $envMap['ADS_BRIDGE_RUNTIME'] = ([System.IO.Path]::GetFullPath($Runtime)).Replace('\', '/')
}

$server = [ordered]@{
    command = $client.Replace('\', '/')
    args = @('-B', ((Join-Path $PSScriptRoot 'mcp_server.py').Replace('\', '/')))
    timeoutSeconds = 60
}
if ($envMap.Count -gt 0) {
    $server['env'] = $envMap
}

$configObj = [ordered]@{
    mcpServers = [ordered]@{
        ads_bridge = $server
    }
}
$jsonText = $configObj | ConvertTo-Json -Depth 6
if ($WriteConfigPath) {
    $dest = [System.IO.Path]::GetFullPath($WriteConfigPath)
    $parent = Split-Path -Parent $dest
    if ($parent -and -not (Test-Path -LiteralPath $parent)) {
        New-Item -ItemType Directory -Path $parent -Force | Out-Null
    }
    Set-Content -LiteralPath $dest -Value $jsonText -Encoding UTF8
    Write-Host "Wrote MCP configuration to $dest" -ForegroundColor Green
}
$jsonText
