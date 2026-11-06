<#
.SYNOPSIS
    Stop the ClaimSleuth dashboard, MCP server and CrewAI A2A service running on the given ports.

.DESCRIPTION
    Finds ClaimSleuth processes by their command line (python -m claimsleuth.dashboard / claimsleuth.mcp_server /
    claimsleuth.crew_service.a2a_server) and their --port (or that module's default port: 8110, 8102, 8103).
    Only processes started from this project's .venv or .venv-crew (and the interpreter process each venv launcher
    starts) are stopped; nothing else is touched. Normally Ctrl+C in the dashboard window is enough (it stops the
    services it started); use this for services started separately or left behind.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\stop_services.ps1
    powershell -ExecutionPolicy Bypass -File scripts\stop_services.ps1 -Ports "8111,8202,8203"
#>
param([string]$Ports = "8110,8102,8103")      # comma-separated (works with -File)

$Root = Split-Path -Parent $PSScriptRoot
$VenvPythons = @((Join-Path $Root ".venv\Scripts\python.exe"), (Join-Path $Root ".venv-crew\Scripts\python.exe"))
$Wanted = @($Ports -split "," | ForEach-Object { [int]$_.Trim() })
$Defaults = @{ "claimsleuth.dashboard" = 8110; "claimsleuth.mcp_server" = 8102; "claimsleuth.crew_service.a2a_server" = 8103 }

$all = @(Get-CimInstance Win32_Process -Filter "Name='python.exe'")
$byId = @{}; foreach ($p in $all) { $byId[[int]$p.ProcessId] = $p }

function Get-Launcher($proc) {
    # The project's venv python.exe itself, or the base interpreter that a venv launcher started.
    if ($VenvPythons -contains $proc.ExecutablePath) { return $proc }
    $parent = $byId[[int]$proc.ParentProcessId]
    if ($parent -and ($VenvPythons -contains $parent.ExecutablePath)) { return $parent }
    return $null
}

$stopped = 0
foreach ($proc in $all) {
    if ($proc.CommandLine -notmatch "-m\s+(claimsleuth\.(dashboard|mcp_server|crew_service\.a2a_server))(\s|$)") { continue }
    $module = $Matches[1]
    $port = if ($proc.CommandLine -match "--port\s+(\d+)") { [int]$Matches[1] } else { $Defaults[$module] }
    if ($Wanted -notcontains $port) { continue }
    $launcher = Get-Launcher $proc
    if ($null -eq $launcher) {
        Write-Host "port ${port}: $module (pid $($proc.ProcessId)) is not from this project's environments; left running" -ForegroundColor Yellow
        continue
    }
    if ($launcher.ProcessId -ne $proc.ProcessId) { continue }     # handled through its venv launcher
    $children = @($all | Where-Object { [int]$_.ParentProcessId -eq [int]$proc.ProcessId -and $_.CommandLine -match [regex]::Escape($module) })
    foreach ($child in $children) { Stop-Process -Id $child.ProcessId -Confirm:$false -ErrorAction SilentlyContinue }
    Stop-Process -Id $proc.ProcessId -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "port ${port}: stopped $module (pid $($proc.ProcessId))"
    $stopped++
}
if ($stopped -eq 0) { Write-Host "No ClaimSleuth process found on port(s) $($Wanted -join ', ')." }
