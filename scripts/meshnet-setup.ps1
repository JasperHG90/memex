# Memex Homelab — Meshnet firewall setup
# Run as Administrator: powershell -ExecutionPolicy Bypass -File scripts\meshnet-setup.ps1

$ErrorActionPreference = "Stop"
$MeshnetRange = "100.64.0.0/10"

Write-Host "`n=== Meshnet Network Info ===" -ForegroundColor Cyan

# Find Meshnet/NordLynx adapter
$meshAdapters = Get-NetIPAddress -AddressFamily IPv4 | Where-Object {
    $_.IPAddress -like "100.64.*" -or $_.InterfaceAlias -like "*Nord*"
}

if ($meshAdapters) {
    foreach ($a in $meshAdapters) {
        Write-Host "  Adapter: $($a.InterfaceAlias)" -ForegroundColor Green
        Write-Host "  Meshnet IP: $($a.IPAddress)" -ForegroundColor Green
    }
} else {
    Write-Host "  No Meshnet adapter found. Is NordVPN Meshnet enabled?" -ForegroundColor Yellow
    Write-Host "  Open NordVPN app > enable Meshnet, then re-run this script." -ForegroundColor Yellow
}

Write-Host "`n=== Creating Firewall Rules ===" -ForegroundColor Cyan

# Define allowed ports (MCP + Dashboard only)
$rules = @(
    @{ Name = "Memex MCP SSE (8081)"; Port = 8081; Desc = "Memex MCP SSE transport for remote Claude Code" },
    @{ Name = "Memex Dashboard (5173)"; Port = 5173; Desc = "Memex React dashboard" }
)

foreach ($rule in $rules) {
    $existing = Get-NetFirewallRule -DisplayName $rule.Name -ErrorAction SilentlyContinue
    if ($existing) {
        Write-Host "  [SKIP] '$($rule.Name)' already exists" -ForegroundColor Yellow
    } else {
        New-NetFirewallRule `
            -DisplayName $rule.Name `
            -Description $rule.Desc `
            -Direction Inbound `
            -Protocol TCP `
            -LocalPort $rule.Port `
            -RemoteAddress $MeshnetRange `
            -Action Allow `
            -Profile Any | Out-Null
        Write-Host "  [OK] Created '$($rule.Name)' (port $($rule.Port), Meshnet only)" -ForegroundColor Green
    }
}

# Block API and DB from Meshnet (defense in depth)
$blockRules = @(
    @{ Name = "Memex API BLOCK (8000)"; Port = 8000; Desc = "Block direct API access from Meshnet" },
    @{ Name = "Memex DB BLOCK (5432)"; Port = 5432; Desc = "Block direct PostgreSQL access from Meshnet" }
)

foreach ($rule in $blockRules) {
    $existing = Get-NetFirewallRule -DisplayName $rule.Name -ErrorAction SilentlyContinue
    if ($existing) {
        Write-Host "  [SKIP] '$($rule.Name)' already exists" -ForegroundColor Yellow
    } else {
        New-NetFirewallRule `
            -DisplayName $rule.Name `
            -Description $rule.Desc `
            -Direction Inbound `
            -Protocol TCP `
            -LocalPort $rule.Port `
            -RemoteAddress $MeshnetRange `
            -Action Block `
            -Profile Any | Out-Null
        Write-Host "  [OK] Created '$($rule.Name)' (BLOCKED from Meshnet)" -ForegroundColor Red
    }
}

Write-Host "`n=== Summary ===" -ForegroundColor Cyan
Write-Host "  Allowed from Meshnet: 8081 (MCP SSE), 5173 (Dashboard)" -ForegroundColor Green
Write-Host "  Blocked from Meshnet: 8000 (API), 5432 (DB)" -ForegroundColor Red
Write-Host "  All ports accessible locally (127.0.0.1)" -ForegroundColor Green

if ($meshAdapters) {
    $ip = $meshAdapters[0].IPAddress
    Write-Host "`n=== Remote Access URLs ===" -ForegroundColor Cyan
    Write-Host "  MCP SSE:   http://${ip}:8081/sse" -ForegroundColor Green
    Write-Host "  Dashboard: http://${ip}:5173" -ForegroundColor Green
    Write-Host "`n  Use the Meshnet IP above in .mcp.json on remote devices."
}

Write-Host ""
