# Start the Figmosha bridge on native Windows, detached.
#
# start-bridge.sh needs bash and tmux, which a plain Windows box has neither of;
# without this the only option is keeping a terminal window open forever.
#
#   .\start-bridge.ps1            # start (no-op if already running)
#   .\start-bridge.ps1 -Restart   # stop whatever holds the port, then start
#   .\start-bridge.ps1 -Stop      # just stop
#
# Logs go to bridge.out.log next to this script.

param(
    [int]$Port = 0,
    [switch]$Restart,
    [switch]$Stop
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$log = Join-Path $root "bridge.out.log"

# The port belongs to the project, not to this script. -Port still overrides,
# for the rare case of running a second bridge by hand.
$projectName = $null
if ($Port -eq 0) {
    $cfgPath = Join-Path $root "project.json"
    if (-not (Test-Path $cfgPath)) {
        Write-Error "no project.json here — claim this copy first: python figmosha.py init --name <Project>"
    }
    $cfg = Get-Content $cfgPath -Raw -Encoding UTF8 | ConvertFrom-Json
    $Port = [int]$cfg.port
    $projectName = $cfg.name
    Write-Host "project «$projectName», port $Port"
}

# Prefer the venv interpreter, fall back to whatever python is on PATH.
$python = Join-Path $root "venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if (-not $cmd) {
        Write-Error "No Python found. Create the venv first: python -m venv venv; .\venv\Scripts\pip install aiohttp"
    }
    $python = $cmd.Source
    Write-Host "venv not found, using $python" -ForegroundColor Yellow
}

function Get-BridgePid {
    # Get-NetTCPConnection returns objects, so there is no netstat text to parse
    # and no dependency on the console locale. It is also asked for one port
    # instead of listing every connection on the machine.
    $conn = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if (-not $conn) { return $null }
    return $conn.OwningProcess
}

function Stop-Bridge {
    $existing = Get-BridgePid
    if (-not $existing) { Write-Host "port $Port is free"; return }
    try {
        Stop-Process -Id $existing -Force -ErrorAction Stop
        Write-Host "stopped pid $existing" -ForegroundColor Green
        Start-Sleep -Milliseconds 600
    } catch {
        Write-Error "could not stop pid ${existing}: $($_.Exception.Message). It may be running as another user — try an elevated shell."
    }
}

if ($Stop -or $Restart) { Stop-Bridge }
if ($Stop) { return }

$existing = Get-BridgePid
if ($existing) {
    Write-Host "bridge already listening on $Port (pid $existing) — use -Restart to replace it" -ForegroundColor Yellow
    return
}

# The path is quoted explicitly: Start-Process does not quote list arguments,
# so a project folder with a space in its name would arrive as two arguments
# and python would look for a module that does not exist.
$bridge = '"' + (Join-Path $root "bridge.py") + '"'
Start-Process -FilePath $python `
    -ArgumentList @("-u", $bridge, "--port", $Port) `
    -WorkingDirectory $root `
    -RedirectStandardOutput $log `
    -RedirectStandardError (Join-Path $root "bridge.err.log") `
    -WindowStyle Hidden | Out-Null

# Poll instead of sleeping a flat 2s: the bridge is usually listening in about
# 300ms, and the wait is paid on every start.
$now = $null
$deadline = (Get-Date).AddSeconds(10)
while (-not $now -and (Get-Date) -lt $deadline) {
    Start-Sleep -Milliseconds 100
    $now = Get-BridgePid
}
if ($now) {
    Write-Host "bridge listening on http://127.0.0.1:$Port (pid $now)" -ForegroundColor Green
    Write-Host "log: $log"
    $menu = if ($projectName) { "Figmosha · $projectName" } else { "Figmosha Bridge" }
    Write-Host "now run the plugin: Figma → Plugins → Development → $menu"
} else {
    Write-Host "bridge did not come up — check $log and bridge.err.log" -ForegroundColor Red
    if (Test-Path $log) { Get-Content $log -Tail 20 }
}
