# Test Meshnet reachability for Memex services
$meshIP = "100.119.180.187"

Write-Host "Testing Memex services on Meshnet IP: $meshIP" -ForegroundColor Cyan

# Test MCP SSE (8081)
Write-Host "`n[MCP SSE :8081]" -ForegroundColor Yellow
try {
    $tcp = New-Object System.Net.Sockets.TcpClient
    $tcp.Connect($meshIP, 8081)
    Write-Host "  REACHABLE - TCP connection successful" -ForegroundColor Green
    $tcp.Close()
} catch {
    Write-Host "  BLOCKED - $($_.Exception.Message)" -ForegroundColor Red
}

# Test Dashboard (5173)
Write-Host "`n[Dashboard :5173]" -ForegroundColor Yellow
try {
    $tcp = New-Object System.Net.Sockets.TcpClient
    $tcp.Connect($meshIP, 5173)
    Write-Host "  REACHABLE - TCP connection successful" -ForegroundColor Green
    $tcp.Close()
} catch {
    Write-Host "  BLOCKED - $($_.Exception.Message)" -ForegroundColor Red
}

# Test API (8000) - should be blocked after firewall rules
Write-Host "`n[API :8000]" -ForegroundColor Yellow
try {
    $tcp = New-Object System.Net.Sockets.TcpClient
    $tcp.Connect($meshIP, 8000)
    Write-Host "  REACHABLE (consider blocking with firewall rules)" -ForegroundColor Yellow
    $tcp.Close()
} catch {
    Write-Host "  BLOCKED (expected)" -ForegroundColor Green
}

# Test DB (5432) - should be blocked after firewall rules
Write-Host "`n[DB :5432]" -ForegroundColor Yellow
try {
    $tcp = New-Object System.Net.Sockets.TcpClient
    $tcp.Connect($meshIP, 5432)
    Write-Host "  REACHABLE (consider blocking with firewall rules)" -ForegroundColor Yellow
    $tcp.Close()
} catch {
    Write-Host "  BLOCKED (expected)" -ForegroundColor Green
}

Write-Host "`n=== Remote .mcp.json config ===" -ForegroundColor Cyan
Write-Host @"
{
  "mcpServers": {
    "memex": {
      "url": "http://${meshIP}:8081/sse"
    }
  }
}
"@ -ForegroundColor Green
