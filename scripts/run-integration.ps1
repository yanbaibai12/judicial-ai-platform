# 一键外部后端联调：
#   1) docker compose -f deploy/docker-compose.backends.yml up -d
#   2) 本脚本加载 .env 中的 MILVUS_URI/NEO4J_URI 并运行集成测试
# 用法：powershell -File scripts/run-integration.ps1
$ErrorActionPreference = "Stop"
Push-Location $PSScriptRoot\..

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Write-Error "未检测到 Docker，请先安装 Docker Desktop 并启动。"
}

Write-Host "==> 启动后端容器（Milvus/Neo4j/Redis）..."
docker compose -f deploy/docker-compose.backends.yml up -d --wait

Write-Host "==> 加载 .env 并运行集成测试..."
Get-Content .env -Encoding utf8 | ForEach-Object {
    if ($_ -match '^\s*([A-Z_][A-Z0-9_]*)\s*=\s*(.*)\s*$' -and $Matches[1] -notin @('ADMIN_TOKEN')) {
        [Environment]::SetEnvironmentVariable($Matches[1], $Matches[2], "Process")
    }
}
$env:INFERENCE_MODE = if ($env:INFERENCE_MODE) { $env:INFERENCE_MODE } else { "mock" }
uv run python -m pytest tests/integration -v

Pop-Location
