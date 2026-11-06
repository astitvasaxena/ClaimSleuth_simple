<#
.SYNOPSIS
    Start ClaimSleuth Simple in LIVE mode: all three agents use genuine Gemini inference.

.DESCRIPTION
    Checks, in order, and stops with a clear message on the first problem:
      1. both virtual environments exist (.venv for MCP, Google ADK and the dashboard; .venv-crew for CrewAI);
      2. a Gemini API key is configured (GOOGLE_API_KEY in the environment or .env; the key is never printed);
      3. CrewAI's Gemini provider (google-genai) is installed in .venv-crew;
      4. the dashboard, MCP and CrewAI ports are free (services already running, for example in offline mode,
         would otherwise be reused and LIVE runs refused).
    Then it starts the dashboard with --mode live. The dashboard starts the MCP server (.venv) and the CrewAI A2A
    service (.venv-crew, Gemini mode) and stops both again when you press Ctrl+C.
    The Google ADK agents (Evidence, Investigator) are not a separate service: they run inside the dashboard
    process when an investigation starts.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\start_live.ps1
    powershell -ExecutionPolicy Bypass -File scripts\start_live.ps1 -Port 8111 -McpPort 8202 -CrewPort 8203
#>
param(
    [int]$Port = 8110,
    [int]$McpPort = 8102,
    [int]$CrewPort = 8103
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$MainPy = Join-Path $Root ".venv\Scripts\python.exe"
$CrewPy = Join-Path $Root ".venv-crew\Scripts\python.exe"

function Fail([string]$Message) {
    Write-Host "LIVE START REFUSED: $Message" -ForegroundColor Red
    exit 1
}

function Test-PortInUse([int]$P) {
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $async = $client.BeginConnect("127.0.0.1", $P, $null, $null)
        if ($async.AsyncWaitHandle.WaitOne(300)) { $client.EndConnect($async); return $true }
        return $false
    } catch { return $false } finally { $client.Close() }
}

Write-Host "ClaimSleuth Simple - LIVE mode (Gemini) startup checks" -ForegroundColor Cyan

# 1. environments
if (-not (Test-Path $MainPy)) { Fail "main environment not found at $MainPy (see README: python -m venv .venv)" }
if (-not (Test-Path $CrewPy)) { Fail "CrewAI environment not found at $CrewPy (see README: python -m venv .venv-crew)" }
Write-Host "  [ok] .venv and .venv-crew found"

# 2. Gemini credentials (checked in Python; prints only the problem, never the key)
Push-Location $Root
try {
    $check = & $MainPy -c "from claimsleuth import execution_mode as m; m.load_env(); p = m.gemini_config_errors(); print(p[0] if p else 'ok:' + m.gemini_model())"
} finally { Pop-Location }
if ($LASTEXITCODE -ne 0 -or -not ($check -like "ok:*")) { Fail "Gemini is not configured. $check" }
$Model = $check.Substring(3)
Write-Host "  [ok] Gemini API key configured (not shown); model $Model"

# 3. CrewAI Gemini provider
& $CrewPy -c "import google.genai, crewai.llms.providers.gemini.completion" 2>$null
if ($LASTEXITCODE -ne 0) { Fail "CrewAI's Gemini provider is missing in .venv-crew. Run: .venv-crew\Scripts\python -m pip install -r requirements\crew.txt" }
Write-Host "  [ok] CrewAI Gemini provider (google-genai) installed in .venv-crew"

# 4. ports
foreach ($pair in @(@("dashboard", $Port), @("MCP server", $McpPort), @("CrewAI A2A service", $CrewPort))) {
    if (Test-PortInUse $pair[1]) {
        Fail "port $($pair[1]) ($($pair[0])) is already in use. Stop the running ClaimSleuth services first: powershell -ExecutionPolicy Bypass -File scripts\stop_services.ps1"
    }
}
Write-Host "  [ok] ports $Port, $McpPort, $CrewPort are free"

# start (foreground; Ctrl+C stops the dashboard and the services it started)
$env:EXECUTION_MODE = "live"
$env:PYTHONIOENCODING = "utf-8"
Write-Host "Starting the dashboard in LIVE mode on http://127.0.0.1:$Port/  (Ctrl+C to stop everything)" -ForegroundColor Green
Push-Location $Root
try {
    & $MainPy -m claimsleuth.dashboard --mode live --port $Port --mcp-port $McpPort --crew-port $CrewPort
    exit $LASTEXITCODE
} finally { Pop-Location }
