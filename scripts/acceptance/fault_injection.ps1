# A08 dependency fault injection driver.
#
# Flow: healthy probe -> stop component -> degraded probe -> start component -> healthy probe.
# Every probe is delegated to run_staging_acceptance.py --scenario a08 so the evidence
# format matches A01/A03/A05.
#
# Usage:
#   powershell -File scripts/acceptance/fault_injection.ps1 -Component milvus -BaseUrl http://127.0.0.1:8000
#   powershell -File scripts/acceptance/fault_injection.ps1 -Component all -Target k8s -Namespace judicial-ai
#
# NOTE: -Target none only records what the platform reports while nothing is stopped;
# use it to capture a baseline or when the dependency is outside this script's control.

param(
    [ValidateSet('milvus', 'neo4j', 'redis', 'inference', 'all')]
    [string]$Component = 'milvus',

    [ValidateSet('docker', 'k8s', 'none')]
    [string]$Target = 'docker',

    [string]$BaseUrl = 'http://127.0.0.1:8000',
    [string]$AdminToken = $env:ADMIN_TOKEN,
    [string]$Namespace = 'judicial-ai',
    [string]$ComposeFile = 'deploy/docker-compose.backends.yml',
    [string]$EvidenceDir = 'evidence',
    [int]$SettleSeconds = 20,
    [string]$Python = '.venv/Scripts/python.exe'
)

$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '../..')).Path
Set-Location $root

if (-not $AdminToken) {
    Write-Error 'AdminToken is required (set ADMIN_TOKEN or pass -AdminToken).'
}

$runner = Join-Path $PSScriptRoot 'run_staging_acceptance.py'
if (-not (Test-Path $Python)) { $Python = 'python' }

function Invoke-Probe {
    param([string]$Expect, [string]$Tag)

    Write-Host "==> probe expect=$Expect tag=$Tag"
    & $Python $runner --base-url $BaseUrl --admin-token $AdminToken `
        --scenario a08 --expect $Expect --tag $Tag --evidence-dir $EvidenceDir
    $code = $LASTEXITCODE
    if ($code -ne 0) {
        Write-Warning "probe '$Tag' reported failures (exit=$code). Evidence is kept for review."
    }
    return $code
}

function Stop-Component {
    param([string]$Name)

    switch ($Target) {
        'docker' {
            Write-Host "==> docker compose stop $Name"
            docker compose -f $ComposeFile stop $Name | Out-Host
        }
        'k8s' {
            Write-Host "==> kubectl scale statefulset/$Name --replicas=0"
            kubectl -n $Namespace scale statefulset $Name --replicas=0 | Out-Host
        }
        default {
            Write-Host "==> target=none, skip stopping $Name"
        }
    }
}

function Start-Component {
    param([string]$Name)

    switch ($Target) {
        'docker' {
            Write-Host "==> docker compose start $Name"
            docker compose -f $ComposeFile start $Name | Out-Host
        }
        'k8s' {
            Write-Host "==> kubectl scale statefulset/$Name --replicas=1"
            kubectl -n $Namespace scale statefulset $Name --replicas=1 | Out-Host
        }
        default {
            Write-Host "==> target=none, skip starting $Name"
        }
    }
}

$components = if ($Component -eq 'all') { @('milvus', 'neo4j', 'redis') } else { @($Component) }
$failures = 0

# Baseline: everything must be healthy before we break anything.
$failures += Invoke-Probe -Expect 'healthy' -Tag "baseline-$($Component)"

foreach ($name in $components) {
    # vLLM is usually outside compose/k8s scope: probe only, never stop it blindly.
    if ($name -eq 'inference') {
        Write-Host '==> inference is not stopped by this script; record current state only.'
        $failures += Invoke-Probe -Expect 'degraded' -Tag 'inference-recorded'
        continue
    }

    Stop-Component -Name $name
    if ($SettleSeconds -gt 0) { Start-Sleep -Seconds $SettleSeconds }

    # The platform must surface an explicit failure or an approved degradation, never a 500.
    $failures += Invoke-Probe -Expect 'degraded' -Tag "$name-down"

    Start-Component -Name $name
    if ($SettleSeconds -gt 0) { Start-Sleep -Seconds $SettleSeconds }

    # Recovery must be clean: if this fails, the dependency did not come back healthy
    # or the platform kept a stale degraded state.
    $failures += Invoke-Probe -Expect 'healthy' -Tag "$name-recovered"
}

Write-Host ''
if ($failures -gt 0) {
    Write-Warning "A08 finished with $failures probe(s) reporting failures. Review evidence before signing off."
    exit 1
}
Write-Host 'A08 probes all passed.'
