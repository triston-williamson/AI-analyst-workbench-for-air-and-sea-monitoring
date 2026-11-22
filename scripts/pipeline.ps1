<#
.SYNOPSIS
  Start, stop or check the Watchfloor pipeline on Windows.

.EXAMPLE
  .\scripts\pipeline.ps1 status
  .\scripts\pipeline.ps1 start
  .\scripts\pipeline.ps1 stop          # also suspends the Snowflake warehouse
#>
param([ValidateSet("start", "stop", "status")][string]$Action = "status")

# "Continue": Windows PowerShell 5.1 turns harmless native stderr (docker progress) into errors.
$ErrorActionPreference = "Continue"
$Root = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Root ".venv\Scripts\python.exe"
$Logs = Join-Path $Root "logs"
$Modules = [ordered]@{
    "ingest.adsb_producer" = "adsb_producer"
    "ingest.ais_producer"  = "ais_producer"
    "load.snowflake_sink"  = "snowflake_sink"
    "detect.runner"        = "runner"
    "dashboard.app"        = "dashboard"
}

function Get-Running {
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
        Where-Object { $_.CommandLine -match 'watchfloor\.(ingest|load|detect|dashboard)' } |
        ForEach-Object { [pscustomobject]@{ Module = ($_.CommandLine -split 'watchfloor\.')[-1].Trim(); Id = $_.ProcessId } }
}

function Show-Status {
    $running = Get-Running | Select-Object -ExpandProperty Module -Unique
    Write-Host "`nProcesses" -ForegroundColor Cyan
    foreach ($m in $Modules.Keys) {
        $log = Join-Path $Logs "$($Modules[$m]).log"
        $age = if (Test-Path $log) { [int]((Get-Date) - (Get-Item $log).LastWriteTime).TotalSeconds } else { $null }
        $state = if ($running -contains $m) { "RUNNING" } else { "STOPPED" }
        $color = if ($state -eq "RUNNING") { "Green" } else { "Red" }
        Write-Host ("  {0,-22} {1,-8} log updated {2}" -f $m, $state, $(if ($null -ne $age) { "${age}s ago" } else { "-" })) -ForegroundColor $color
    }
    Write-Host "`nKafka" -ForegroundColor Cyan
    docker ps --filter name=watchfloor --format "  {{.Names}}: {{.Status}}"
    Write-Host "`nData freshness (Snowflake)" -ForegroundColor Cyan
    & $Python -m watchfloor.ops health 2>$null
    Write-Host "`nDashboard: http://127.0.0.1:8050   Kafka UI: http://127.0.0.1:8080`n"
}

switch ($Action) {
    "start" {
        Push-Location $Root
        docker compose up -d 2>$null | Out-Null
        $running = Get-Running | Select-Object -ExpandProperty Module -Unique
        foreach ($m in $Modules.Keys) {
            if ($running -contains $m) { Write-Host "  $m already running"; continue }
            $name = $Modules[$m]
            Start-Process -WindowStyle Hidden -FilePath $Python -ArgumentList "-m", "watchfloor.$m" `
                -WorkingDirectory $Root `
                -RedirectStandardError (Join-Path $Logs "$name.stderr.log") `
                -RedirectStandardOutput (Join-Path $Logs "$name.stdout.log") | Out-Null
            Write-Host "  started $m"
        }
        Pop-Location
        Start-Sleep 5
        Show-Status
    }
    "stop" {
        Get-Running | ForEach-Object { Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue }
        Push-Location $Root
        docker compose stop 2>$null | Out-Null
        & $Python -m watchfloor.ops suspend 2>$null
        Pop-Location
        Write-Host "  pipeline stopped"
    }
    "status" { Show-Status }
}
